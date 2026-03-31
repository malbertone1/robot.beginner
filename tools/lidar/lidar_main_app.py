import time
import threading
import math
import serial
from flask import Flask, render_template
from flask_socketio import SocketIO
from pyrplidar import PyRPlidar

app = Flask(__name__)
# Added ping_timeout to prevent the Pi from dropping the web connection
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', ping_timeout=10)

# --- CONFIG ---
PORT = '/dev/ttyUSB0'
BAUD = 460800
LIDAR_OFFSET_X = 100 
ANGLE_OFFSET = 0 

params = {'avg': 5, 'sens': 60, 'persist': 3}
known_map = []      
tracking_map = {}   
is_scanning = True  

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('update_params')
def handle_params(data):
    global params, known_map, tracking_map
    for key, value in data.items():
        params[key] = int(value)
    known_map = []; tracking_map = {}

@socketio.on('toggle_motor')
def handle_motor(data):
    global is_scanning
    is_scanning = data['state']
    print(f"Scanning state changed to: {is_scanning}")

def lidar_worker():
    global known_map, tracking_map, is_scanning
    
    while True:
        if not is_scanning:
            socketio.emit('lidar_status', {'status': 'stopped'})
            time.sleep(1)
            continue

        lidar = PyRPlidar()
        try:
            socketio.emit('lidar_status', {'status': 'connecting'})
            
            # Reset USB Buffer
            with serial.Serial(PORT, BAUD, timeout=1) as ser:
                ser.setDTR(False); ser.setRTS(False)
                time.sleep(0.2)
                ser.reset_input_buffer()
                ser.setDTR(True); ser.setRTS(True)
                time.sleep(0.5)

            lidar.connect(port=PORT, baudrate=BAUD, timeout=3)
            lidar.set_motor_pwm(660)
            socketio.emit('lidar_status', {'status': 'spinning_up'})
            time.sleep(2) 
            
            scan_generator = lidar.start_scan()
            socketio.emit('lidar_status', {'status': 'running'})
            
            scan_history = []
            current_accumulator = []
            last_angle = 0
            
            for scan in scan_generator():
                if not is_scanning: break
                now = time.time()
                
                if scan.angle < last_angle:
                    if current_accumulator:
                        dists = [math.sqrt(p['x']**2 + p['y']**2) for p in current_accumulator]
                        min_v = min(dists); max_v = max(dists)
                        min_idx = dists.index(min_v); max_idx = dists.index(max_v)
                        
                        metrics = {
                            'min': round(min_v), 'max': round(max_v),
                            'min_p': current_accumulator[min_idx],
                            'max_p': current_accumulator[max_idx]
                        }
                        
                        scan_history.append(current_accumulator)
                        if len(scan_history) > params['avg']: scan_history.pop(0)
                        
                        socketio.emit('lidar_data', {
                            'points': [p for s in scan_history for p in s],
                            'metrics': metrics
                        })
                    
                    current_accumulator = []
                    known_map = [p for p in known_map if now - p[2] < 10]
                    socketio.sleep(0)

                if scan.distance > 0:
                    rad = math.radians(scan.angle + ANGLE_OFFSET + 90)
                    x = -(scan.distance * math.cos(rad))
                    y = (scan.distance * math.sin(rad)) + LIDAR_OFFSET_X
                    
                    is_stable = False
                    for k_x, k_y, k_ts in known_map:
                        if abs(x - k_x) < params['sens'] and abs(y - k_y) < params['sens']:
                            is_stable = True; break
                    
                    current_accumulator.append({'x': x, 'y': y, 'isNew': not is_stable})
                    
                    if not is_stable:
                        grid_pos = (round(x/40)*40, round(y/40)*40)
                        if grid_pos not in tracking_map: 
                            tracking_map[grid_pos] = now
                        elif now - tracking_map[grid_pos] > params['persist']:
                            known_map.append((x, y, now))
                
                last_angle = scan.angle

        except Exception as e:
            print(f"LIDAR ERROR: {e}")
            socketio.emit('lidar_status', {'status': f'Error: {str(e)[:20]}'})
            time.sleep(2)
        finally:
            try:
                lidar.stop(); lidar.disconnect()
            except: pass

if __name__ == '__main__':
    # Start the worker thread BEFORE the app runs
    t = threading.Thread(target=lidar_worker, daemon=True)
    t.start()
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
