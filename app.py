# System level imports
import os, io, logging, json, time, re, glob
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
Picamera2.set_logging(Picamera2.WARNING)
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
camera_profile_folder = os.path.join(current_dir, 'static/camera_profiles')
app.config['camera_profile_folder'] = camera_profile_folder
# Create the upload folder if it doesn't exist
os.makedirs(app.config['camera_profile_folder'], exist_ok=True)

# Set the path where the images will be stored for the image gallery
upload_folder = os.path.join(current_dir, 'static/gallery')
app.config['upload_folder'] = upload_folder

# For the image gallery set items per page
items_per_page = 12

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
# Streaming Class and function
####################

class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.buffer = io.BytesIO()
        self.condition = Condition()

    def write(self, buf):
        # Clear the buffer before writing the new frame
        self.buffer.seek(0)
        self.buffer.truncate()
        self.buffer.write(buf)
        with self.condition:
            self.condition.notify_all()

    def read_frame(self):
        self.buffer.seek(0)
        return self.buffer.read()

# Define a function to generate the stream for a specific camera
def generate_stream(camera):
    while True:
        with camera.output.condition:
            camera.output.condition.wait()  # Wait for the new frame to be available
            frame = camera.output.read_frame()
        if frame:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

####################
# CameraObject that will store the itteration of 1 or more cameras
####################

class CameraObject:
    def __init__(self, camera):
        self.camera_info = camera
        self.camera_profile = self.generate_camera_profile()
        # Init camera to picamera2 using the camera number
        self.picam2 = Picamera2(camera['Num'])
        # Initialize configs as empty dictionaries
        self.still_config = {}
        self.video_config = {}
        # Get camera specs
        self.camera_module_spec = self.get_camera_module_spec()
        # Basic Camera Info (Sensor type etc)
        self.sensor_modes = self.picam2.sensor_modes
        self.configure_camera()
        self.set_sensor_mode(self.camera_profile["sensor_mode"])
        self.live_controls = self.initialize_controls_template(self.picam2.camera_controls)
        metadata = self.picam2.capture_metadata()
        print(f"Metadata: {metadata}")
        print(f"Camera Controls: {self.picam2.camera_controls}")
        print("Active Streams:", self.picam2.streams)
        self.output = None
        self.start_streaming()

    def capture_metadata(self):
        self.metadata = self.picam2.capture_metadata()
        print(f"Metadata: {self.metadata}")
        return self.metadata

    def apply_profile_controls(self):
        if "controls" in self.camera_profile:
            try:
                for setting_id, setting_value in self.camera_profile["controls"].items():
                    self.picam2.set_controls({setting_id: setting_value})
                    print(f"Applied {setting_id} -> {setting_value}")
                    time.sleep(0.5)  # Small delay between each setting

                print("✅ All profile controls applied with delay")
                self.live_controls = self.camera_profile['controls']
            except Exception as e:
                print(f"⚠️ Error applying profile controls: {e}")

    def update_exposure_from_metadata(self):
        metadata = self.capture_metadata()
        print(metadata)
        if metadata:
            exposure_time = metadata.get("ExposureTime")
            analogue_gain = metadata.get("AnalogueGain")
            
            if exposure_time is not None:
                self.camera_profile["controls"]["ExposureTime"] = exposure_time
                self.update_settings("ExposureTime", exposure_time)
                print(f"Updated ExposureTime: {exposure_time}")
            
            if analogue_gain is not None:
                self.camera_profile["controls"]["AnalogueGain"] = analogue_gain
                self.update_settings("AnalogueGain", analogue_gain)
                print(f"Updated AnalogueGain: {analogue_gain}")
        else:
            print("Failed to fetch metadata")

    def reset_to_default(self):
        # Resets camera settings to default and applies them.
        self.camera_profile = {
            "hflip": 0,
            "vflip": 0,
            "sensor_mode": 0,
            "live_preview": True,
            "model": self.camera_info.get("Model", "Unknown"),
            "controls": {}  # Empty controls to be updated later
        }

        # Reset key settings
        self.set_sensor_mode(self.camera_profile["sensor_mode"])
        self.set_orientation()

        # Reinitialize UI settings
        self.live_controls = self.initialize_controls_template(self.picam2.camera_controls)
        print(self.live_controls)
        self.update_exposure_from_metadata()
        # Apply the default settings using the new function

        self.apply_profile_controls()

        print("Camera profile reset to default and settings applied.")
    
    def generate_camera_profile(self):
        file_name = os.path.join(camera_profile_folder, 'camera-module-info.json')

        # If there is no existing config, or the file doesn't exist, create a default profile
        if not self.camera_info.get("Has_Config", False) or not os.path.exists(file_name):
            self.camera_profile = {
                "hflip": 0,
                "vflip": 0,
                "sensor_mode": 0,
                "live_preview": True,
                "model": self.camera_info.get("Model", "Unknown"),
                "controls": {}
            }
        else:
            # Load existing profile from file
            with open(file_name, 'r') as file:
                self.camera_profile = json.load(file)

        return self.camera_profile

    def start_streaming(self):
        self.output = StreamingOutput()
        self.picam2.start_recording(MJPEGEncoder(), output=FileOutput(self.output), name='main')
        print("[INFO] Streaming started")
        time.sleep(1)

    def stop_streaming(self):
        if self.output:  # Ensure streaming was started before stopping
            self.picam2.stop_recording()
            print("[INFO] Streaming stopped")

    def get_camera_module_spec(self):
        # Find and return the camera module details based on the sensor model.
        camera_module = next((cam for cam in camera_module_info["camera_modules"] if cam["sensor_model"] == self.camera_info["Model"]), None)
        return camera_module

    def update_camera_config(self):
        self.picam2.stop()
        self.set_orientation()
        self.picam2.create_still_configuration()
        self.picam2.create_video_configuration()
        self.picam2.configure(self.still_config)
        self.picam2.configure(self.video_config)
        self.picam2.start()

    def configure_camera(self, config=None):
        self.picam2.stop()
        self.set_still_config(config)
        self.set_video_config(config)
        self.picam2.start()

    def set_still_config(self, config=None):
        self.still_config = self.picam2.create_still_configuration(**(config or {}))
        self.set_orientation()
        self.picam2.configure(self.still_config)

    def set_video_config(self, config=None):
        self.video_config = self.picam2.create_video_configuration(**(config or {}))
        self.set_orientation()
        self.picam2.configure(self.video_config)

    def update_video_config(self, config=None):
        self.picam2.configure(self.video_config)

    def initialize_controls_template(self, picamera2_controls):
        with open("camera_controls_db.json", "r") as f:
            camera_json = json.load(f)

        if "sections" not in camera_json:
            print("Error: 'sections' key not found in camera_json!")
            return camera_json  # Return unchanged if it's not structured as expected

        # Initialize empty controls in camera_profile
        self.camera_profile["controls"] = {}

        for section in camera_json["sections"]:
            if "settings" not in section:
                print(f"Warning: Missing 'settings' key in section: {section.get('title', 'Unknown')}")
                continue
            
            section_enabled = False  # Track if any setting is enabled

            for setting in section["settings"]:
                if not isinstance(setting, dict):
                    print(f"Warning: Unexpected setting format: {setting}")
                    continue  # Skip if it's not a dictionary

                setting_id = setting.get("id")  # Use `.get()` to avoid crashes
                source = setting.get("source", None)  # Check if source exists
                original_enabled = setting.get("enabled", False)  # Preserve original enabled state

                # If source is "controls", validate against picamera2_controls
                if source == "controls":
                    if setting_id in picamera2_controls:
                        min_val, max_val, default_val = picamera2_controls[setting_id]
                        print(f"Updating {setting_id}: Min={min_val}, Max={max_val}, Default={default_val}")  # Debugging

                        setting["min"] = min_val
                        setting["max"] = max_val
                        if default_val is not None:
                            setting["default"] = default_val
                        else:
                            # Use fallback default value if missing
                            default_val = False if isinstance(min_val, bool) else min_val
                        
                        if setting["enabled"]:
                            # Ensure it is stored in the camera profile
                             self.camera_profile["controls"][setting_id] = default_val

                        # Preserve original enabled state
                        setting["enabled"] = original_enabled  

                        # Mark section as enabled only if at least one setting is enabled
                        if original_enabled:
                            section_enabled = True  
                    
                    else:
                        print(f"Disabling {setting_id}: Not found in picamera2_controls")  # Debugging
                        setting["enabled"] = False  # Disable setting
                
                else:
                    # If the setting does not have "source: controls", keep it unchanged
                    print(f"Skipping {setting_id}: No source specified, keeping existing values.")
                    section_enabled = True  # Since at least one setting remains, keep the section

                # Check and update child settings (dependencies)
                if "dependencies" in setting:
                    for child in setting["dependencies"]:
                        child_id = child.get("id")
                        child_source = child.get("source", None)

                        if child_source == "controls" and child_id in picamera2_controls:
                            min_val, max_val, default_val = picamera2_controls[child_id]
                            print(f"Updating Child {child_id}: Min={min_val}, Max={max_val}, Default={default_val}")  # Debugging

                            child["min"] = min_val
                            child["max"] = max_val

                            # ✅ Always store child settings in camera_profile, even if no default is provided
                            self.camera_profile["controls"][child_id] = default_val if default_val is not None else min_val

                            # ✅ Ensure the child setting is properly stored
                            if default_val is not None:
                                child["default"] = default_val  

                            # Preserve original enabled state
                            child["enabled"] = child.get("enabled", False)

                            if child["enabled"]:
                                section_enabled = True  # Mark section as enabled
                        else:
                            print(f"Skipping or Disabling Child {child_id}: Not found or no source specified")

            # If all settings in a section are disabled, disable the section itself
            section["enabled"] = section_enabled

        print(f"Initialized camera_profile controls: {self.camera_profile['controls']}")
        return camera_json

    def update_settings(self, setting_id, setting_value):
        print(f"Updating setting: {setting_id} -> {setting_value}")
        
        # Handle sensor mode separately
        if setting_id == "sensor-mode":
            try:
                self.set_sensor_mode(setting_value)
                self.camera_profile['sensor_mode'] = setting_value
                print(f"Sensor mode {setting_value} applied")
            except ValueError as e:
                print(f"⚠️ Error: {e}")

        # Handle hflip and vflip separately
        elif setting_id in ["hflip", "vflip"]:
            try:
                self.camera_profile[setting_id] = bool(int(setting_value))
                self.update_camera_config()
                print(f"Applied transform: {setting_id} -> {setting_value} (Camera restarted)")
            except ValueError as e:
                print(f"⚠️ Error: {e}")

        else:
            # Convert setting_value to correct type
            if "." in str(setting_value):
                setting_value = float(setting_value)
            else:
                setting_value = int(setting_value)

            # Apply the setting
            self.picam2.set_controls({setting_id: setting_value})

            # Store in camera_profile["controls"]
            self.camera_profile.setdefault("controls", {})[setting_id] = setting_value

        # Update live settings
        updated = False
        for section in self.live_controls.get("sections", []):
            for setting in section.get("settings", []):
                if setting["id"] == setting_id:
                    setting["value"] = setting_value  # Update main setting
                    updated = True
                    break

                # Check child settings
                for child in setting.get("childsettings", []):
                    if child["id"] == setting_id:
                        child["value"] = setting_value  # Update child setting
                        updated = True
                        break

            if updated:
                break  # Exit loop once found

        if not updated:
            print(f"⚠️ Warning: Setting {setting_id} not found in live_controls!")

        print(f"Stored setting: {setting_id} -> {setting_value}")

        metadata = self.picam2.capture_metadata()
        print(metadata)

        return setting_value  # Returning for confirmation

    def set_sensor_mode(self, mode_index):
        # Ensure setting_value is an integer (mode index)
        mode_index = int(mode_index)
        if mode_index < 0 or mode_index >= len(self.sensor_modes):
            raise ValueError("Invalid sensor mode index")

        mode = self.sensor_modes[mode_index]
        self.camera_profile["sensor_mode"] = mode_index 
        # Create the configuration dictionary
        config = {'sensor': {'output_size': mode['size'], 'bit_depth': mode['bit_depth']}}

        # Reconfigure the camera with the new sensor mode
        self.configure_camera(config)
    
    def get_sensor_mode(self):
        current_config = self.picam2.camera_configuration()
        active_mode = current_config.get('sensor', {})  # Get the currently active sensor settings

        active_mode_index = None  # Default to None if no match is found

        # Find the matching sensor mode index
        for index, mode in enumerate(self.sensor_modes):
            if mode['size'] == active_mode.get('output_size') and mode['bit_depth'] == active_mode.get('bit_depth'):
                active_mode_index = index
                break

        return active_mode_index

    def set_orientation(self):
        # Get current transform settings
        transform = Transform()

        # Apply hflip and vflip from camera_profile
        transform.hflip = self.camera_profile.get("hflip", False)
        transform.vflip = self.camera_profile.get("vflip", False)

        # Update both video and still configs
        self.still_config['transform'] = transform
        self.video_config['transform'] = transform

        print("Applied Orientation - hflip:", transform.hflip, "vflip:", transform.vflip)

    def take_still(self, camera_num, image_name):
        try:
            # self.stop_streaming()
            filepath = os.path.join(app.config['upload_folder'], image_name)

            # Ensure we are using still capture mode
            # self.picam2.switch_mode_and_capture_file(self.still_config, f"{filepath}.jpg")
            # self.picam2.capture_file(f"{filepath}.jpg")
            request = self.picam2.capture_request()
            request.save("main", f'{filepath}.jpg')

            logging.info(f"Image captured successfully. Path: {filepath}")

            # Explicitly reset the video mode
            # self.start_streaming()
            # generate_stream(self.picam2)

            return f'{filepath}.jpg'
        except Exception as e:
            logging.error(f"Error capturing image: {e}")
            return None

    def save_profile(self, filename):
        try:
            # Ensure .json is not already in the filename
            if filename.lower().endswith(".json"):
                filename = filename[:-5]

            filename = os.path.join(camera_profile_folder, filename)
            with open(f"{filename}.json", "w") as f:
                json.dump(self.camera_profile, f, indent=4)
            return True
        except Exception as e:
            print(f"Error saving profile: {e}")
            return False

####################
# ImageGallery Class
####################

class ImageGallery:
    def __init__(self, upload_folder, items_per_page=10):
        self.upload_folder = upload_folder
        self.items_per_page = items_per_page
        self.items_per_page = 12

    def get_image_files(self):
        """Fetch image file details, including timestamps, resolution, and DNG presence."""
        try:
            image_files = [f for f in os.listdir(self.upload_folder) if f.endswith('.jpg')]
            files_and_timestamps = []

            for image_file in image_files:
                # Extract timestamp from filename
                try:
                    unix_timestamp = int(image_file.split('_')[-1].split('.')[0])
                    timestamp = datetime.utcfromtimestamp(unix_timestamp).strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    logging.warning(f"Skipping file {image_file} due to incorrect timestamp format")
                    continue  # Skip files with incorrect format

                # Check if corresponding .dng file exists
                dng_file = os.path.splitext(image_file)[0] + '.dng'
                has_dng = os.path.exists(os.path.join(self.upload_folder, dng_file))

                # Get image resolution
                img_path = os.path.join(self.upload_folder, image_file)
                with Image.open(img_path) as img:
                    width, height = img.size

                # Append file details
                files_and_timestamps.append({
                    'filename': image_file,
                    'timestamp': timestamp,
                    'has_dng': has_dng,
                    'dng_file': dng_file,
                    'width': width,
                    'height': height
                })

            # Sort files by timestamp (newest first)
            files_and_timestamps.sort(key=lambda x: x['timestamp'], reverse=True)
            return files_and_timestamps

        except Exception as e:
            logging.error(f"Error loading image files: {e}")
            return []

    def paginate_images(self, page):
        """Paginate images dynamically after an image is deleted."""
        all_images = self.get_image_files()
        
        # Recalculate total pages dynamically
        total_pages = max((len(all_images) + self.items_per_page - 1) // self.items_per_page, 1)

        # Adjust the current page if necessary
        if page > total_pages:
            page = total_pages  # Ensure we're not on a non-existent page

        start_index = (page - 1) * self.items_per_page
        end_index = start_index + self.items_per_page
        paginated_images = all_images[start_index:end_index]

        return paginated_images, total_pages
    

    def find_last_image_taken(self):
        """Find the most recent image taken."""
        all_images = self.get_image_files()
        
        if all_images:
            first_image = all_images[0]
            print(f"Filename: {first_image['filename']}")
            image = first_image['filename']
        else:
            print("No image files found.")
            image = None
        
        return image  # Extract only the filename
    
    def delete_image(self, filename):
        image_path = os.path.join(app.config['upload_folder'], filename)

        if os.path.exists(image_path):
            try:
                os.remove(image_path)
                logging.info(f"Deleted image: {filename}")
                return True, f"Image '{filename}' deleted successfully."
            except Exception as e:
                logging.error(f"Error deleting image {filename}: {e}")
                return False, "Failed to delete image"
        else:
            return False, "Image not found"



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
# WebUI routes 
####################

@app.context_processor
def inject_theme():
    theme = session.get('theme', 'light')  # Default to 'light'
    return dict(version=version, title=project_title, theme=theme)

@app.context_processor
def inject_camera_list():
    camera_list = [(camera.camera_info, get_camera_info(camera.camera_info['Model'], camera_module_info)) 
                   for key, camera in cameras.items()]
    return dict(camera_list=camera_list)

@app.route('/set_theme/<theme>')
def set_theme(theme):
    session['theme'] = theme
    return jsonify(success=True, ok=True, message="Theme updated successfully")

# Define 'home' route
@app.route('/')
def home():
    camera_list = [(camera.camera_info, get_camera_info(camera.camera_info['Model'], camera_module_info)) for key, camera in cameras.items()]
    return render_template('home.html', active_page='home')

@app.route('/camera_info_<int:camera_num>')
def camera_info(camera_num):
    # Check if the camera number exists
    camera = cameras.get(camera_num)
    if not camera:
        return render_template('error.html', message="Error: Camera not found"), 404
    # Get camera module spec
    camera_module_spec = camera.camera_module_spec

    return render_template('camera_info.html', camera_data=camera_module_spec, camera_num=camera_num)

@app.route("/about")
def about():
    return render_template("about.html", active_page='about')

####################
# Camera Control routes 
####################

@app.route("/camera_mobile_<int:camera_num>")
def camera_mobile(camera_num):
    try:
        camera = cameras.get(camera_num)
        if not camera:
            return render_template('camera_not_found.html', camera_num=camera_num)

        # Get camera settings
        live_controls = camera.live_controls
   
        sensor_modes = camera.sensor_modes

        active_mode_index = camera.get_sensor_mode()

        # Find the last image taken by this specific camera
        last_image = None
        last_image = image_gallery_manager.find_last_image_taken()

        return render_template('camera_mobile.html', camera=camera.camera_info, settings=live_controls, sensor_modes=sensor_modes, active_mode_index=active_mode_index, last_image=last_image)
    
    except Exception as e:
        logging.error(f"Error loading camera view: {e}")
        return render_template('error.html', error=str(e))

@app.route("/camera_<int:camera_num>")
def camera(camera_num):
    try:
        camera = cameras.get(camera_num)
        if not camera:
            return render_template('camera_not_found.html', camera_num=camera_num)

        # Get camera settings
        live_controls = camera.live_controls
   
        sensor_modes = camera.sensor_modes

        active_mode_index = camera.get_sensor_mode()

        # Find the last image taken by this specific camera
        last_image = None
        last_image = image_gallery_manager.find_last_image_taken()

        return render_template('camera.html', camera=camera.camera_info, settings=live_controls, sensor_modes=sensor_modes, active_mode_index=active_mode_index, last_image=last_image)
    
    except Exception as e:
        logging.error(f"Error loading camera view: {e}")
        return render_template('error.html', error=str(e))

# Dictionary to track the last capture time per camera
last_capture_time = {}

@app.route("/capture_still_<int:camera_num>", methods=["POST"])
def capture_still(camera_num):
    global last_capture_time

    try:
        logging.debug(f"📸 Received capture request for camera {camera_num}")

        camera = cameras.get(camera_num)
        if not camera:
            logging.warning(f"❌ Camera {camera_num} not found.")
            return jsonify(success=False, message="Camera not found"), 404

        # Rate limit: Prevent captures happening too quickly (2 seconds per camera)
        current_time = time.time()
        #if camera_num in last_capture_time and (current_time - last_capture_time[camera_num]) < 2:
        #   logging.warning(f"⚠️ Capture request too fast for camera {camera_num}. Ignoring request.")
        #   return jsonify(success=False, message="Capture request too fast"), 429  # Too Many Requests

        # Update the last capture time for this camera
        last_capture_time[camera_num] = current_time

        # Generate the new filename
        timestamp = int(time.time())  # Current Unix timestamp
        image_filename = f"pimage_camera_{camera_num}_{timestamp}"
        logging.debug(f"📁 New image filename: {image_filename}")

        # Capture and save the new image
        image_path = camera.take_still(camera_num, image_filename)

        # Add a slight delay to prevent overlapping captures
        time.sleep(0.5)

        if image_path:
            logging.info(f"✅ Image captured successfully: {image_filename}")
            return jsonify(success=True, message="Image captured successfully", image=image_filename)
        else:
            logging.error(f"❌ Failed to capture image for camera {camera_num}")
            return jsonify(success=False, message="Failed to capture image")

    except Exception as e:
        logging.error(f"🔥 Error capturing still image: {e}")
        return jsonify(success=False, message=str(e)), 500

@app.route('/video_feed_<int:camera_num>')
def video_feed(camera_num):
    camera = cameras.get(camera_num)
    if camera:
        return Response(generate_stream(camera), mimetype='multipart/x-mixed-replace; boundary=frame')
    else:
        abort(404)

@app.route("/toggle_video_feed", methods=["POST"])
def toggle_video_feed():
    data = request.json
    enable = data.get("enable", False)
    camera_num = data.get("camera_num")

    if camera_num is None:
        return jsonify({"success": False, "error": "Invalid camera number"}), 400

    camera_num = int(camera_num)

    if camera_num in cameras:
        if enable:
            cameras[camera_num].start_streaming()
        else:
            cameras[camera_num].stop_streaming()
        return jsonify({"success": True})
    
    return jsonify({"success": False, "error": "Camera not found"}), 404

@app.route('/preview_<int:camera_num>', methods=['POST'])
def preview(camera_num):
    try:
        camera = cameras.get(camera_num)
        if camera:
            filepath = f'snapshot/pimage_preview_{camera_num}'
            preview_path = camera.take_still(camera_num, filepath)
            return jsonify(success=True, message="Photo captured successfully", image_path=preview_path)
    except Exception as e:
        return jsonify(success=False, message=str(e))

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


@app.route('/camera_controls')
def redirect_to_home():
    return redirect(url_for('home'))

####################
# Camera Profile routes 
####################

@app.route('/save_profile_<int:camera_num>', methods=['POST'])
def save_profile(camera_num):
    data = request.json
    filename = data.get("filename")

    if not filename:
        return jsonify({"error": "Filename is required"}), 400
    camera = cameras.get(camera_num)
    success = camera.save_profile(filename)

    if success:
        return jsonify({"message": f"Profile '{filename}' saved successfully"}), 200
    else:
        return jsonify({"error": "Failed to save profile"}), 500

@app.route("/reset_profile_<int:camera_num>", methods=["POST"])
def reset_profile(camera_num):
    if camera_num not in cameras:
        return jsonify({"success": False, "message": "Camera not found"}), 404

    camera = cameras[camera_num]
    camera.reset_to_default()
    return jsonify({"success": True, "message": "Profile reset to default"})

@app.route("/fetch_metadata_<int:camera_num>")
def fetch_metadata(camera_num):
    if camera_num not in cameras:
        return jsonify({"error": "Invalid camera number"}), 400
    camera = cameras[camera_num]
    metadata = camera.capture_metadata()  # Get metadata for the selected camera
    print(f"Camera {camera_num} Metadata: {metadata}")  # Log metadata
    return jsonify(metadata)  # Return as JSON

####################
# Image gallery routes 
####################

# Initialize the gallery with the upload folder
image_gallery_manager = ImageGallery(upload_folder)

@app.route('/image_gallery')
def image_gallery():
    page = request.args.get('page', 1, type=int)
    images, total_pages = image_gallery_manager.paginate_images(page)

    cameras_data = [(camera_num, camera) for camera_num, camera in cameras.items()]

    if not images:
        return render_template('no_files.html')

    # Define pagination bounds
    start_page = max(1, page - 2)  # Show previous 2 pages
    end_page = min(total_pages, page + 2)  # Show next 2 pages

    return render_template(
        'image_gallery.html',
        image_files=images,
        page=page,
        total_pages=total_pages,
        start_page=start_page,
        end_page=end_page,
        cameras_data=cameras_data,
        active_page='image_gallery'
    )

@app.route('/view_image/<filename>')
def view_image(filename):
    image_path = os.path.join(app.config['upload_folder'], filename)

    if os.path.exists(image_path):
        return send_file(image_path)
    else:
        return render_template("error.html", message="Image not found"), 404

@app.route('/delete_image/<filename>', methods=['DELETE'])
def delete_image(filename):
    success, message = image_gallery_manager.delete_image(filename)

    if success:
        return jsonify({"success": True, "message": message}), 200
    else:
        return jsonify({"success": False, "message": message}), 404 if "not found" in message else 500

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