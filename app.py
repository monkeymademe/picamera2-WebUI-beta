# System level imports
import os, io, logging, json, time, re
from datetime import datetime
from threading import Condition
import threading
import argparse

# Flask imports
from flask import Flask, render_template, request, jsonify, Response, send_file, abort, session, redirect, url_for
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
# Uncomment the line below simulate having no cameras connected
# global_cameras = []
print(f'\nInitialize picamera2 - Cameras Found:\n{global_cameras}\n')

####################
# Initialize default values 
####################

version = "1.0.6 - BETA"
project_title = "Picamera2 WebUI"

# Get the directory of the current script
current_dir = os.path.dirname(os.path.abspath(__file__))

# Set the path where the images will be stored
CAMERA_CONFIG_FOLDER = os.path.join(current_dir, 'static/camera_config')
app.config['CAMERA_CONFIG_FOLDER'] = CAMERA_CONFIG_FOLDER
# Create the upload folder if it doesn't exist
os.makedirs(app.config['CAMERA_CONFIG_FOLDER'], exist_ok=True)

# Set the path where the images will be stored
UPLOAD_FOLDER = os.path.join(current_dir, 'static/gallery')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Define the minimum required configuration
minimum_last_config = {
    "cameras": []
}

last_config_file_path = os.path.join(current_dir, 'camera-last-config.json')

# Load the camera-module-info.json file
with open(os.path.join(current_dir, 'camera-module-info.json'), 'r') as file:
    camera_module_info = json.load(file)


# Function to load or initialize configuration
def load_or_initialize_config(file_path, default_config):
    if os.path.exists(file_path):
        with open(file_path, 'r') as file:
            try:
                config = json.load(file)
                if not config:  # Check if the file is empty
                    raise ValueError("Empty configuration file")
            except (json.JSONDecodeError, ValueError):
                # If file is empty or invalid, create new config
                with open(file_path, 'w') as file:
                    json.dump(default_config, file, indent=4)
                config = default_config
    else:
        # Create the file with minimum configuration if it doesn't exist
        with open(file_path, 'w') as file:
            json.dump(default_config, file, indent=4)
        config = default_config
    return config

def control_template():
    with open("camera_controls_db.json", "r") as f:
        settings = json.load(f)
    return settings

# Load or initialize the configuration
camera_last_config = load_or_initialize_config(last_config_file_path, minimum_last_config)

def get_camera_info(camera_model, camera_module_info):
    return next(
        (module for module in camera_module_info["camera_modules"] if module["sensor_model"] == camera_model),
        next(module for module in camera_module_info["camera_modules"] if module["sensor_model"] == "Unknown")
    )

####################
# CameraObject that will store the itteration of 1 or more cameras
####################

class CameraObject:
    def __init__(self, camera):
        self.camera_info = camera
        # Init camera to picamera2 using the camera number
        self.picam2 = Picamera2(camera['Num'])
        # Basic Camera Info (Sensor type etc)
        self.set_still_config()
        self.set_video_config()
        self.picam2.start()
        self.test = self.picam2.camera_controls
        print(self.test)
        self.live_settings = self.initialize_controls_template(self.picam2.camera_controls)
        print(self.live_settings)

    def set_still_config(self):
        self.still_config = self.picam2.create_still_configuration()
        self.picam2.configure(self.still_config)

    def set_video_config(self):
        self.video_config = self.picam2.create_video_configuration()
        self.picam2.configure(self.video_config)

    def initialize_controls_template(self, picamera2_controls):
        with open("camera_controls_db.json", "r") as f:
            camera_json = json.load(f)
    
        if "sections" not in camera_json:
                print("Error: 'sections' key not found in camera_json!")
                return camera_json  # Return unchanged if it's not structured as expected

        for section in camera_json["sections"]:
            if "settings" not in section:
                print(f"Warning: Missing 'settings' key in section: {section.get('title', 'Unknown')}")
                continue
            
            for setting in section["settings"]:
                if not isinstance(setting, dict):
                    print(f"Warning: Unexpected setting format: {setting}")
                    continue  # Skip if it's not a dictionary

                setting_id = setting.get("id")  # Use `.get()` to avoid crashes
                
                # Update main setting
                if setting_id in picamera2_controls:
                    min_val, max_val, default_val = picamera2_controls[setting_id]
                    print(f"Updating {setting_id}: Min={min_val}, Max={max_val}, Default={default_val}")  # Debugging

                    setting["min"] = min_val
                    setting["max"] = max_val
                    if default_val is not None:
                        setting["default"] = default_val  # Only update if there's a default

                    # Preserve the "enabled" state inside checkboxes or radio buttons
                    if "options" in setting:
                        for option in setting["options"]:
                            option["enabled"] = option.get("enabled", False)  # Keep disabled options disabled
                        
                else:
                    print(f"Skipping {setting_id}: Not found in picamera2_controls")  # Debugging

                # Check and update child settings (dependencies)
                if "dependencies" in setting:
                    for child in setting["dependencies"]:
                        child_id = child.get("id")
                                        
                        if child_id in picamera2_controls:
                            min_val, max_val, default_val = picamera2_controls[child_id]
                            print(f"Updating Child {child_id}: Min={min_val}, Max={max_val}, Default={default_val}")  # Debugging

                            child["min"] = min_val
                            child["max"] = max_val
                            if default_val is not None:
                                child["default"] = default_val  # Only update if there's a default

                            # Preserve the "enabled" state inside checkboxes or radio buttons
                            if "options" in child:
                                for option in child["options"]:
                                    option["enabled"] = option.get("enabled", False)  # Keep disabled options disabled

                        else:
                            print(f"Skipping Child {child_id}: Not found in picamera2_controls")  # Debugging

        return camera_json

    def update_settings(self, setting_id, setting_value):
        print(f"Updating setting: {setting_id} -> {setting_value}")

        # Handle hflip and vflip separately
        if setting_id in ["hflip", "vflip"]:
            # Stop the camera before updating transform settings
            self.picam2.stop()

            # Get current transform settings or create a new one
            transform = self.video_config.get('transform', Transform())

            # Apply the new flip setting
            setattr(transform, setting_id, bool(int(setting_value)))  # Ensure True/False

            # Update both video and still configs
            self.video_config['transform'] = transform
            self.still_config['transform'] = transform

            # Reconfigure the camera
            self.picam2.configure(self.video_config)
            self.picam2.configure(self.still_config)

            # Restart the camera
            self.picam2.start()

            print(f"Applied transform: {setting_id} -> {setting_value} (Camera restarted)")

        else:
            # Convert setting_value to correct type only for normal settings
            if "." in str(setting_value):
                setting_value = float(setting_value)
            else:
                setting_value = int(setting_value)

            # Apply the setting normally
            self.picam2.set_controls({setting_id: setting_value})

        # Update live settings
        updated = False
        for section in self.live_settings.get("sections", []):
            for setting in section.get("settings", []):
                if setting["id"] == setting_id:
                    setting["value"] = setting_value  # Update main setting
                    updated = True
                    break  # Stop searching once found

                # Check child settings
                for child in setting.get("dependencies", []):
                    if child["id"] == setting_id:
                        child["value"] = setting_value  # Update child setting
                        updated = True
                        break  # Stop searching once found

            if updated:
                break  # Exit outer loop

        if not updated:
            print(f"⚠️ Warning: Setting {setting_id} not found in live_settings!")

        print(f"Stored setting: {setting_id} -> {setting_value}")

        return setting_value  # Returning for confirmation

    def take_preview(self,camera_num):
        try:
            image_name = f'snapshot/pimage_preview_{camera_num}'
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], image_name)
            request = self.picam2.capture_request()
            request.save("main", f'{filepath}.jpg')
            logging.info(f"Image captured successfully. Path: {filepath}")
            return f'{filepath}.jpg'
        except Exception as e:
            logging.error(f"Error capturing image: {e}")


####################
# Cycle through Cameras to create connected camera config
####################

# Template for a new config which will be the new camera-last-config
currently_connected_cameras = {'cameras': []}
# Iterate over each camera in the global_cameras list building a config model
for connected_camera in global_cameras:   
    # Check if the connected camera is a Raspberry Pi Camera Module
    matching_module = next(
        (module for module in camera_module_info["camera_modules"] 
         if module["sensor_model"] == connected_camera["Model"]), 
        None
    )
    if matching_module and matching_module.get("is_pi_cam", False) is True:
        print(f"Connected camera model '{connected_camera['Model']}' is found in the camera-module-info.json and is a Pi Camera.\n")
        is_pi_cam = True
    else:
        print(f"Connected camera model '{connected_camera['Model']}' is either NOT in the camera-module-info.json or is NOT a Pi Camera.\n")
        is_pi_cam = False
    # Build usable Connected Camera Information variable
    camera_info = {'Num':connected_camera['Num'], 'Model':connected_camera['Model'], 'Is_Pi_Cam': is_pi_cam, 'Has_Config': False, 'Config_Location': f"default_{connected_camera['Model']}.json"}
    currently_connected_cameras['cameras'].append(camera_info)

# Create a lookup for existing cameras by "Num"
existing_cameras_lookup = {cam["Num"]: cam for cam in camera_last_config["cameras"]}
# Prepare the updated list of cameras
updated_cameras = []

# Compare config generated from global_cameras with what was last connected and update the camera-last-config
for new_cam in currently_connected_cameras["cameras"]:
    cam_num = new_cam["Num"]
    if cam_num in existing_cameras_lookup:
        old_cam = existing_cameras_lookup[cam_num]  
        # If the camera model has changed, update it
        if old_cam["Model"] != new_cam["Model"]:
            print(f"Updating camera {new_cam['Model']}: Model or Pi Cam status changed.")
            updated_cameras.append(new_cam)
        else:
            # Keep existing config if nothing changed
            updated_cameras.append(old_cam)
    else:
        # If it's a new camera, add it to the list
        print(f"New camera added to config: {new_cam}")
        updated_cameras.append(new_cam)

# Save the updated configuration
new_config = {"cameras": updated_cameras}
with open(os.path.join(current_dir, 'camera-last-config.json'), "w") as file:
    json.dump(new_config, file, indent=4)

# Make sure currently_connected_cameras is the definitively list of connected cameras
currently_connected_cameras = updated_cameras

print(f"\n\n{currently_connected_cameras}\n\n ")

####################
# Cycle through connected cameras and generate camera object
####################

cameras = {}

for connected_camera in currently_connected_cameras:
    camera_obj = CameraObject(connected_camera)
    cameras[connected_camera['Num']] = camera_obj
    print(f"\n\n{cameras}\n\n ")

for key, camera in cameras.items():
    print(f"Key: {key}, Camera: {camera.camera_info}")



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
    camera_list = [(camera.camera_info, get_camera_info(camera.camera_info['Model'], camera_module_info)) for key, camera in cameras.items()]
    print(camera_list)
    return render_template('home.html', active_page='home', camera_list=camera_list)

@app.route('/preview_<int:camera_num>', methods=['POST'])
def preview(camera_num):
    try:
        camera = cameras.get(camera_num) 
        print(camera)
        if camera:
            # Capture an image
            filepath = camera.take_preview(camera_num)
            # Wait for a few seconds to ensure the image is saved
            time.sleep(1)
            return jsonify(success=True, message="Photo captured successfully")
    except Exception as e:
        return jsonify(success=False, message=str(e))

@app.route("/camera_controls_<int:camera_num>")
def camera_controls(camera_num):
    try:
        camera = cameras.get(camera_num)
        live_settings = camera.live_settings
        return render_template('camera_controls.html', camera=camera.camera_info, settings=live_settings)
    except Exception as e:
        # TODO: Make a template for camera not found
        return render_template('camera_controls.html', camera=camera, settings=live_settings)

@app.route('/update_setting', methods=['POST'])
def update_setting():
    try:
        data = request.json  # Get JSON data from the request
        camera_num = data.get("camera_num")  # New field for camera selection
        setting_id = data.get("id")
        new_value = data.get("value")
        # Debugging: Print the received values
        print(f"Received update for Camera {camera_num}: {setting_id} -> {new_value}")
        camera = cameras.get(camera_num)
        camera.update_settings(setting_id, new_value)
        # ✅ At this stage, we're just verifying the data. No changes to the camera yet.
        return jsonify({
            "success": True,
            "message": f"Received setting update for Camera {camera_num}: {setting_id} -> {new_value}"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/about")
def about():
    return render_template("about.html", active_page='about')

settings = control_template()

@app.route('/camera_controls')
def redirect_to_home():
    return redirect(url_for('home'))


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