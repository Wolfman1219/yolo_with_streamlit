import os
import cv2
import warnings
from collections import namedtuple
from pathlib import Path
from typing import List, Optional, Tuple, Union
import pycuda.autoinit
import numpy as np
import pycuda.driver as cuda
context = cuda.Device(0).make_context()
import tensorrt as trt
from numpy import ndarray
# import atexit

class TRTEngine:
    def __init__(self, weight: Union[str, Path]) -> None:
        self.weight = Path(weight) if isinstance(weight, str) else weight
        self.stream = cuda.Stream(0)
        self.__init_engine()
        self.__init_bindings()
        self.__warm_up()

    def __init_engine(self) -> None:
        logger = trt.Logger(trt.Logger.WARNING)
        trt.init_libnvinfer_plugins(logger, namespace='')
        with trt.Runtime(logger) as runtime:
            model = runtime.deserialize_cuda_engine(self.weight.read_bytes())

        context = model.create_execution_context()

        names = [model.get_binding_name(i) for i in range(model.num_bindings)]
        self.num_bindings = model.num_bindings
        self.bindings: List[int] = [0] * self.num_bindings
        num_inputs, num_outputs = 0, 0

        for i in range(model.num_bindings):
            if model.binding_is_input(i):
                num_inputs += 1
            else:
                num_outputs += 1

        self.num_inputs = num_inputs
        self.num_outputs = num_outputs
        self.model = model
        self.context = context
        self.input_names = names[:num_inputs]
        self.output_names = names[num_inputs:]

    def __init_bindings(self) -> None:
        dynamic = False
        Tensor = namedtuple('Tensor', ('name', 'dtype', 'shape', 'cpu', 'gpu'))
        inp_info = []
        out_info = []
        out_ptrs = []
        for i, name in enumerate(self.input_names):
            assert self.model.get_binding_name(i) == name
            dtype = trt.nptype(self.model.get_binding_dtype(i))
            shape = tuple(self.model.get_binding_shape(i))
            if -1 in shape:
                dynamic |= True
            if not dynamic:
                cpu = np.empty(shape, dtype)
                gpu = cuda.mem_alloc(cpu.nbytes)
                cuda.memcpy_htod_async(gpu, cpu, self.stream)
            else:
                cpu, gpu = np.empty(0), 0
            inp_info.append(Tensor(name, dtype, shape, cpu, gpu))
        for i, name in enumerate(self.output_names):
            i += self.num_inputs
            assert self.model.get_binding_name(i) == name
            dtype = trt.nptype(self.model.get_binding_dtype(i))
            shape = tuple(self.model.get_binding_shape(i))
            if not dynamic:
                cpu = np.empty(shape, dtype=dtype)
                gpu = cuda.mem_alloc(cpu.nbytes)
                cuda.memcpy_htod_async(gpu, cpu, self.stream)
                out_ptrs.append(gpu)
            else:
                cpu, gpu = np.empty(0), 0
            out_info.append(Tensor(name, dtype, shape, cpu, gpu))

        self.is_dynamic = dynamic
        self.inp_info = inp_info
        self.out_info = out_info
        self.out_ptrs = out_ptrs

    def __warm_up(self) -> None:
        if self.is_dynamic:
            print('You engine has dynamic axes, please warm up by yourself !')
            return
        for _ in range(10):
            inputs = []
            for i in self.inp_info:
                inputs.append(i.cpu)
            self.__call__(inputs)

    def set_profiler(self, profiler: Optional[trt.IProfiler]) -> None:
        self.context.profiler = profiler \
            if profiler is not None else trt.Profiler()

    def __call__(self, *inputs) -> Tuple[ndarray, ndarray, ndarray, ndarray]:

        assert len(inputs) == self.num_inputs
        contiguous_inputs: List[ndarray] = [
            np.ascontiguousarray(i) for i in inputs
        ]

        for i in range(self.num_inputs):

            if self.is_dynamic:
                self.context.set_binding_shape(
                    i, tuple(contiguous_inputs[i].shape))
                self.inp_info[i].gpu = cuda.mem_alloc(
                    contiguous_inputs[i].nbytes)

            cuda.memcpy_htod_async(self.inp_info[i].gpu, contiguous_inputs[i],
                                   self.stream)
            self.bindings[i] = int(self.inp_info[i].gpu)

        output_gpu_ptrs: List[int] = []
        outputs: List[ndarray] = []

        for i in range(self.num_outputs):
            j = i + self.num_inputs
            if self.is_dynamic:
                shape = tuple(self.context.get_binding_shape(j))
                dtype = self.out_info[i].dtype
                cpu = np.empty(shape, dtype=dtype)
                gpu = cuda.mem_alloc(contiguous_inputs[i].nbytes)
                cuda.memcpy_htod_async(gpu, cpu, self.stream)
            else:
                cpu = self.out_info[i].cpu
                gpu = self.out_info[i].gpu
            outputs.append(cpu)
            output_gpu_ptrs.append(gpu)
            self.bindings[j] = int(gpu)
        self.context.execute_async_v2(self.bindings, self.stream.handle)
        self.stream.synchronize()

        for i, o in enumerate(output_gpu_ptrs):
            cuda.memcpy_dtoh_async(outputs[i], o, self.stream)
        
        data_output = tuple(outputs) if len(outputs) > 1 else outputs[0]
        num_dets, bboxes, scores, labels = (i[0] for i in data_output)
        nums = num_dets.item()
        bboxes = bboxes[:nums]
        scores = scores[:nums]
        labels = labels[:nums]
        
        return bboxes, scores, labels
def letterbox(im: ndarray,
              new_shape: Union[Tuple, List] = (640, 640),
              color: Union[Tuple, List] = (0, 0, 0)) \
        -> Tuple[ndarray, float, Tuple[float, float]]:
    # Resize and pad image while meeting stride-multiple constraints
    shape = im.shape[:2]  # current shape [height, width]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)

    # Scale ratio (new / old)
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])

    # Compute padding
    new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
    dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[
        1]  # wh padding

    dw /= 2  # divide padding into 2 sides
    dh /= 2

    if shape[::-1] != new_unpad:  # resize
        im = cv2.resize(im, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im = cv2.copyMakeBorder(im,top,bottom,left,right,cv2.BORDER_CONSTANT,value=color)  # add border
    
    return im, r, (dw, dh)
def letterbox(im: ndarray,
              new_shape: Union[Tuple, List] = (640, 640),
              color: Union[Tuple, List] = (0, 0, 0)) \
        -> Tuple[ndarray, float, Tuple[float, float]]:
    # Resize and pad image while meeting stride-multiple constraints
    shape = im.shape[:2]  # current shape [height, width]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)

    # Scale ratio (new / old)
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])

    # Compute padding
    new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
    dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[
        1]  # wh padding

    dw /= 2  # divide padding into 2 sides
    dh /= 2

    if shape[::-1] != new_unpad:  # resize
        im = cv2.resize(im, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im = cv2.copyMakeBorder(im,top,bottom,left,right,cv2.BORDER_CONSTANT,value=color)  # add border
    return im, r, (dw, dh)

def blob(im: ndarray, return_seg: bool = False) -> Union[ndarray, Tuple]:
    if return_seg:
        seg = im.astype(np.float32) / 255
    im = im.transpose([2, 0, 1])
    im = im[np.newaxis, ...]
    im = np.ascontiguousarray(im).astype(np.float32) / 255
    if return_seg:
        return im, seg
    else:
        return im

def run_tensorrt(enggine_path, image):
    enggine = TRTEngine(enggine_path)

    H, W = enggine.inp_info[0].shape[-2:]
    bgr, ratio, dwdh = letterbox(image, (W, H))
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    tensor = blob(rgb, return_seg=False)
    dwdh = np.array(dwdh * 2, dtype=np.float32)
    tensor = np.ascontiguousarray(tensor)

    # Detection
    results = enggine(tensor)
    bboxes, scores, labels = results
    bboxes -= dwdh
    bboxes /= ratio

    # CLASSES = ('person','car','car', 'car', 'car','car', 'car','car','car', 'car','car','car','car','car')
    CLASSES = {0: 'person',1: 'bicycle',2: 'car',3: 'motorcycle',4: 'airplane',5: 'bus',6: 'train',7: 'truck',8: 'boat',9: 'traffic light',10: 'fire hydrant',11: 'stop sign',12: 'parking meter',13: 'bench',14: 'bird',15: 'cat',16: 'dog',17: 'horse',18: 'sheep',19: 'cow',
            20: 'elephant',21: 'bear',22: 'zebra',23: 'giraffe',24: 'backpack',25: 'umbrella',26: 'handbag',27: 'tie',28: 'suitcase',29: 'frisbee',30: 'skis',31: 'snowboard',32: 'sports ball',33: 'kite',34: 'baseball bat',35: 'baseball glove',36: 'skateboard',37: 'surfboard',38: 'tennis racket',39: 'bottle',
            40: 'wine glass',41: 'cup',42: 'fork',43: 'knife',44: 'spoon',45: 'bowl',46: 'banana',47: 'apple',48: 'sandwich',49: 'orange',50: 'broccoli',51: 'carrot',52: 'hot dog',53: 'pizza',54: 'donut',55: 'cake',56: 'chair',57: 'couch',58: 'potted plant',59: 'bed',
            60: 'dining table',61: 'toilet',62: 'tv',63: 'laptop',64: 'mouse',65: 'remote',66: 'keyboard',67: 'cell phone',68: 'microwave',69: 'oven',70: 'toaster',71: 'sink',72: 'refrigerator',73: 'book',74: 'clock',75: 'vase',76: 'scissors',77: 'teddy bear',78: 'hair drier',  79: 'toothbrush'}

    for (bbox, score, label) in zip(bboxes, scores, labels):
        bbox = bbox.round().astype(np.int32).tolist()
        cls_id = int(label)
        cls = CLASSES[cls_id]
        color = (0,255,0)
        cv2.rectangle(image, tuple(bbox[:2]), tuple(bbox[2:]), color, 2)
        cv2.putText(image,
                f'{cls}:{score:.3f}', (bbox[0], bbox[1] - 2),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.75, [225, 255, 255],
                thickness=2)
        # cv2.imwrite("output.jpg", image)
    return image
context.pop()