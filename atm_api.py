from flask import Flask, request, jsonify
from final_threaded_with_destination import (
    MetroAPI, TripPlanner, load_lines_from_file,
    update_lines_with_candidates, Line, Station
)
import time
import logging
from typing import Dict, Any, List

# --------------------------------------------------------------------------
# Logging Configuration
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("atm_api.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Global variables for caching
_lines = None
_metro_api = None
_planner = None

def initialize_planner():
    """Initialize the planner with the lines data if not already done."""
    global _lines, _metro_api, _planner
    
    if _lines is None:
        logger.info("Initializing planner with lines data")
        lines_file = "/Users/andre/Desktop/Coding/Python/tenv/Projects/atm/lines.json"
        _lines = load_lines_from_file(lines_file)
        _metro_api = MetroAPI(max_workers=10)
        _planner = TripPlanner(_lines, _metro_api)
        logger.info("Planner initialized successfully")

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "timestamp": time.time()
    })

@app.route('/plan', methods=['POST'])
def plan_trip():
    """
    Plan a trip based on provided start and end candidates.
    Expected JSON body:
    {
        "start_candidates": [
            {
                "line_code": "15",
                "direction": "0",
                "target_station_code": "15371",
                "walking_time": 8
            },
            ...
        ],
        "end_candidates": [
            {
                "line_code": "15",
                "direction": "0",
                "target_station_code": "15379",
                "walking_time": 4
            },
            ...
        ]
    }
    """
    try:
        # Initialize planner if needed
        initialize_planner()
        
        # Get request data
        data = request.get_json()
        if not data or 'start_candidates' not in data or 'end_candidates' not in data:
            return jsonify({
                "error": "Missing start_candidates or end_candidates arrays in request body"
            }), 400
            
        start_candidates = data['start_candidates']
        end_candidates = data['end_candidates']
        
        # Convert candidates to the format expected by update_lines_with_candidates
        start_candidates_dict = {
            str(c['line_code']): {
                "direction": str(c['direction']),
                "target_station_code": str(c['target_station_code']),
                "walking_time": int(c['walking_time'])
            }
            for c in start_candidates
        }
        
        end_candidates_dict = {
            str(c['line_code']): {
                "direction": str(c['direction']),
                "target_station_code": str(c['target_station_code']),
                "walking_time": int(c['walking_time'])
            }
            for c in end_candidates
        }
        
        # Update lines with candidates
        update_lines_with_candidates(_lines, start_candidates_dict, end_candidates_dict)
        
        # Get trip plan
        start_time = time.time()
        trip_plan = _planner.plan_trip()
        best_tram = _planner.best_tram()
        end_time = time.time()
        
        # Format response
        response = {
            "execution_time": round(end_time - start_time, 2),
            "feasible_options": {},
            "best_option": None
        }
        
        # Format feasible options
        for station_line, tram_infos in trip_plan.items():
            response["feasible_options"][station_line] = [
                {
                    "arrival": round(info['arrival'], 1),
                    "feasible": info['feasible'],
                    "walk_time": info['walk_time'],
                    "wait_at_stop": info['wait_at_stop'],
                    "raw_wait": info['raw_wait'],
                    "station_idx": info['station_idx'],
                    "total_travel_time": round(info['total_travel_time'], 1),
                    "final_walking_time": info['final_walking_time'],
                    "total_time": round(info['total_time'], 1)
                }
                for info in tram_infos
            ]
        
        # Format best option
        if best_tram:
            sl = best_tram["station_line"]
            tinfo = best_tram["tram"]
            response["best_option"] = {
                "station_line": sl,
                "arrival": round(tinfo['arrival'], 1),
                "walk_time": tinfo['walk_time'],
                "wait_at_stop": tinfo['wait_at_stop'],
                "total_travel_time": round(tinfo['total_travel_time'], 1),
                "final_walking_time": tinfo['final_walking_time'],
                "total_time": round(tinfo['total_time'], 1)
            }
        
        return jsonify(response)
        
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        return jsonify({
            "error": f"Internal server error: {str(e)}"
        }), 500

@app.route('/lines', methods=['GET'])
def get_lines():
    """Get information about all available lines."""
    try:
        initialize_planner()
        
        lines_info = []
        for line in _lines:
            active_station = next((s for s in line.stations if s.active), None)
            line_info = {
                "code": line.line_code,
                "name": line.name,
                "direction": line.direction,
                "active_station": {
                    "name": active_station.name,
                    "code": active_station.code,
                    "walking_time": active_station.walking_time
                } if active_station else None
            }
            lines_info.append(line_info)
            
        return jsonify({
            "lines": lines_info
        })
        
    except Exception as e:
        logger.error(f"Error getting lines info: {str(e)}")
        return jsonify({
            "error": f"Internal server error: {str(e)}"
        }), 500

if __name__ == '__main__':
    # Initialize the planner when starting the server
    initialize_planner()
    
    # Run the Flask app
    app.run(host='0.0.0.0', port=3001, debug=True) 