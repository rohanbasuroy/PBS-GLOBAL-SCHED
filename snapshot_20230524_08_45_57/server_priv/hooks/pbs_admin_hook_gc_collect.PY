#!/usr/bin/env python3
# Designed and created from toonen@alcf.anl.gov and pershey@anl.gov
import gc
import pbs
import traceback

event_obj = pbs.event()
try:
    gc.collect()
    gc_objects_len = len(gc.get_objects())
    gc_garbage_len = len(gc.garbage)
    pbs.logmsg(pbs.LOG_ERROR, f"{event_obj.hook_name}: len(objs):{gc_objects_len}, len(garbage):{gc_garbage_len}")
    event_obj.accept()
except SystemExit:
    raise
except:
    error_lines = traceback.format_exc().strip().splitlines()
    for error_line_idx, error_str in enumerate(error_lines):
        pbs.logmsg(pbs.LOG_ERROR, f"{event_obj.hook_name}:Exception(L#{error_line_idx}):{error_str}")
    event_obj.accept()
