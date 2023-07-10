## PBS Global Scheduler
This work uses DIM_JOB_COMPOSITE data of ALCF systems to implement different scheduling techniques. It convers any ALCF log into PBS format, and uses PBS simulator in the backend to develop scheduling strategies. Also, in a given timeframe, it can determine the most optimal scheduling startegy/ sorting formula to aid system admins. The usage is as follows:

```
python3 simsh.py <machine_name> <simulation_start_timestamp> <simulation_end_timestamp>

```

An example usage is:
```
python3 simsh.py theta 2019-12-10 19:00:00 2019-12-14 19:00:00
```