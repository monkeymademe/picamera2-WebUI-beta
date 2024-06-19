import os, io, logging, json, time, re
from datetime import datetime
from threading import Condition
import threading

from flask import Flask, render_template, request, jsonify, Response, send_file, abort

from PIL import Image

from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput
from libcamera import Transform, controls

# Init Flask
app = Flask(__name__)

# Get global camera information
global_cameras = Picamera2.global_camera_info()

# Get the directory of the current script
current_dir = os.path.dirname(os.path.abspath(__file__))
# Define the path to the camera-config.json file
camera_config_path = os.path.join(current_dir, 'camera-config.json')

# Load the camera-module-info.json file
with open(os.path.join(current_dir, 'camera-module-info.json'), 'r') as file:
    camera_module_info = json.load(file)

# Load the JSON configuration file
with open(os.path.join(current_dir, 'camera-last-config.json'), 'r') as file:
    camera_config = json.load(file)

# CameraObject that will store the iteration of 1 or more cameras
class CameraObject:
    def __init__(self, camera_num, camera_info):
        self.camera = Picamera2(camera_num)
        self.settings = self.camera.camera_controls
        self.sensor_modes = self.camera.sensor_modes
        self.camera_num = 
        # Update model and other information based on global camera info
        self.update_camera_info(camera_info)

    def update_camera_info(self, camera_info, camera_num):
        for info in camera_info:
            if info['Num'] == self.camera.camera_num:
                self.model = info['Model']
                # Check if the camera is listed in camera-module-info.json
                # Assuming the existence of camera_module_info dictionary
                for module_info in camera_module_info:
                    if module_info["num"] == info["Num"] and module_info["model"] == info["Model"]:
                        self.ispicam = module_info["ispicam"]
                break

# Init dictionary to store camera instances
cameras = {}

# Iterate over each camera
for camera_info in global_cameras:
    # Get the camera number
    camera_num = camera_info['Num']
    # Create an instance of the custom CameraObject class
    camera_obj = CameraObject(camera_num, global_cameras)
    # Start the camera
    camera_obj.camera.start()
    # Add the camera instance to the dictionary
    cameras[camera_num] = camera_obj

# Print camera information
for num, camera in cameras.items():
    print(f"Camera {num}: Model={camera.model}, ISPicam={camera.ispicam}, Settings={camera.settings}, Sensor Modes={camera.sensor_modes}")
