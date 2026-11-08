"""Discovery utilities for recognizing CIB devices from a .pub mapping file."""

import re
from typing import Dict, List, Set

from .models import PubEntry
from .pubfile import load_pub_entries

def discover_cib_devices_from_pub(pub_file_path: str) -> Dict[str, Set[str]]:
    """
    Parse a .pub file and extract a mapping of hardware modules (like CIB racks)
    to the variables they control. This helps in recognizing physically connected
    devices without requiring proprietary EPSNET file transfer commands.
    """
    entries = load_pub_entries(pub_file_path)
    modules: Dict[str, Set[str]] = {}
    
    for entry in entries:
        # Common patterns: INPUT_R2_IN_1, OUTPUT_R1a_light, SW_living_room_UP1
        # The CIB bus modules are typically identified by their Rack/Bus designation
        # such as R0, R1, R1a, R1b, R2, R2a, R2b
        
        # Match standard INELS/Tecomat module prefixes
        match = re.match(r'^(?:INPUT|OUTPUT|DIMMER|state)_(R[0-9a-z]+)_', entry.name, re.IGNORECASE)
        if match:
            module_id = match.group(1).upper()
            modules.setdefault(module_id, set()).add(entry.name)
            continue
            
        # Match switches and logical groups that might not have R prefix but indicate topology
        match_sw = re.match(r'^(SW|C_IT)_(.*?)_', entry.name, re.IGNORECASE)
        if match_sw:
            module_id = f"Group_{match_sw.group(2).upper()}"
            modules.setdefault(module_id, set()).add(entry.name)
            
    return modules

def print_cib_topology(pub_file_path: str) -> None:
    """Print the discovered CIB hardware topology to the console."""
    try:
        modules = discover_cib_devices_from_pub(pub_file_path)
        print(f"--- Detected CIB Topology from {pub_file_path} ---")
        if not modules:
            print("No recognizable CIB hardware modules found in the mapping.")
            return
            
        for module, variables in sorted(modules.items()):
            print(f"\nModule/Group: {module} ({len(variables)} variables)")
            # Print a sample of up to 5 variables to help the user identify it
            sample = sorted(list(variables))[:5]
            for var in sample:
                print(f"  - {var}")
            if len(variables) > 5:
                print(f"  ... and {len(variables) - 5} more")
    except Exception as err:
        print(f"Failed to parse topology: {err}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print_cib_topology(sys.argv[1])
    else:
        print("Usage: python3 discovery.py <path_to.pub>")
