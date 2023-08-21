import streamlit as st
import cv2
import numpy as np
from PIL import Image
import tempfile

# Import your TensorRT function
from TensorRT_uchun import run_tensorrt

# Set your default video path
DEFAULT_VIDEO_PATH = "/home/hasan/Public/yolo_with_streamlit/data/sample_videos/sample.mp4"

# Create a video file uploader
st.header("Upload a video for inference")
uploaded_file = st.file_uploader("Choose a video...", type=["mp4", "avi", "mov"])

# Create a radio button for selecting between default video and uploaded video
video_selection = st.radio(
    "Select video for inference:",
    ("Use default video", "Use uploaded video")
)

# If the user chooses to use the default video
if video_selection == "Use default video":
    video_path = DEFAULT_VIDEO_PATH

# If the user chooses to use the uploaded video
elif video_selection == "Use uploaded video" and uploaded_file is not None:
    tfile = tempfile.NamedTemporaryFile(delete=False) 
    tfile.write(uploaded_file.read())
    video_path = tfile.name

# If there's a video to process, do the inference
if video_path is not None:
    # Load the video with cv2
    cap = cv2.VideoCapture(video_path)

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        # Run the inference
        output = run_tensorrt(image=frame, enggine_path='models/yolov8n.engine')

        # Convert the output to an image that can be displayed
        output_image = Image.fromarray(output)

        # Display the image
        st.image(output_image)

    cap.release()
else:
    st.write("Please upload a video file or choose to use the default video.")