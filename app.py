# System level imports
import os, io, logging, json, time, re
from datetime import datetime
from threading import Condition
import threading
import argparse

# Flask imports
from flask import Flask, render_template, request, jsonify, Response, send_file, abort, session
import secrets

# picamera2 imports
from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.encoders import MJPEGEncoder
#from picamera2.encoders import H264Encoder
from picamera2.outputs import FileOutput
from libcamera import Transform, controls

# Image handeling imports
from PIL import Image

####################
# Initialize Flask 
####################

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)  # Generates a random 32-character hexadecimal string
# https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Set-Cookie#samesitesamesite-value
app.config["SESSION_COOKIE_SAMESITE"] = "None"

####################
# Initialize picamera2 
####################

# Set debug level
Picamera2.set_logging(Picamera2.DEBUG)
# Ask picamera2 for what cameras are connected
###### Might want to put something here that deals with no cameras connected?
global_cameras = Picamera2.global_camera_info()
# Uncomment the line below if you want to limt the number of cameras connected (change the number to index which camera you want)
# global_cameras = [global_cameras[0]]
print(f'\nInitialize picamera2 - Cameras Found:\n{global_cameras}\n')

####################
# Initialize default values 
####################

version = "1.0.6 - BETA"
project_title = "Picamera2 WebUI"

# Get the directory of the current script
current_dir = os.path.dirname(os.path.abspath(__file__))

connected_cameras_config = os.path.join(current_dir, 'connected_cameras_config.json')

def load_connected_cameras_config():
    if os.path.exists(connected_cameras_config):
        # Load existing config
        with open(connected_cameras_config, 'r') as file:
            config = json.load(file)
            print("Loaded configuration:", config)
            return config
    else:
        # Create default config file
        with open(connected_cameras_config, 'w') as file:
            json.dump(default_config, file, indent=4)
        print("Created default configuration file.")
        return default_config



####################
# CameraObject that will store the itteration of 1 or more cameras
####################

class CameraObject:
    def __init__(self, camera_num, camera_info):

####################
# Cycle through Cameras to create Class Object
####################

# Initialize dictionary to store camera instances
cameras = {}
connect_cameras = {'cameras': []}

# Iterate over each camera in the global_cameras list
for camera_info in global_cameras:
    # Flag to check if a matching camera is found in the last config
    matching_camera_found = False
    print(f'\nInitialize Camera:\n{camera_info}\n')

    # Get the number of the camera in the global_cameras list
    camera_num = camera_info['Num']

####################
# Flask routes 
####################

@app.context_processor
def inject_theme():
    theme = session.get('theme', 'light')  # Default to 'light'
    return dict(version=version, title=project_title, theme=theme)

@app.route('/set_theme/<theme>')
def set_theme(theme):
    session['theme'] = theme
    return jsonify(success=True, ok=True, message="Theme updated successfully")

# Define 'home' route
@app.route('/')
def home():
    return render_template('home.html', active_page='home')

@app.route("/about")
def about():
    return render_template("about.html", active_page='about')


####################
# Start Flask 
####################

if __name__ == "__main__":
    # Parse any argument passed from command line
    parser = argparse.ArgumentParser(description='PiCamera2 WebUI')
    parser.add_argument('--port', type=int, default=8080, help='Port number to run the web server on')
    parser.add_argument('--ip', type=str, default='0.0.0.0', help='IP to which the web server is bound to')
    args = parser.parse_args()
    # If there are no arguments the port will be 8080 and ip 0.0.0.0 
    app.run(host=args.ip, port=args.port)