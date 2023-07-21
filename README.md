# PBS Global Scheduler
### Description of the tool
PBS Global Scheduler uses DIM_JOB_COMPOSITE data of ALCF systems to implement different scheduling techniques. It converts any ALCF log (PBS or non-PBS) into PBS format, and uses PBS simulator in the backend to develop scheduling strategies. Also, in a given timeframe, it can determine the most optimal scheduling startegy/ sorting formula to aid system admins. Users can use to simulate scheduling of real ALCF job invocation logs with different scheduling sorting formula. Also, system admins can utilize the optimal scheduling formula, as determined by this tool, to configure the systems for real time scheduling. Currently, this repo contains the DIM_JOB_COMPOSITE logs of MIRA, POLARIS, THETA, THETAGPU, COOLEY, and INTREPID. When the DIM_JOB_COMPOSITE data of the later ALCF systems become available, it nees to be extracted in the "./data" folder for simulation.

### Installation
This tool uses PBS simulator (already present in this repo), in the backend, to perform the simulations. For this to work, the following PBS executables needs to be in path: *pbs_est, pbs_rstat, pbs_rsub, pbs_sim, pbs_nodes, qdel, qmgr, qselect, qstat, qsub, tracejob*. Also, the following environment variable needs to be set: **SIM_LICENSE_LOCATION=6200@license-polaris-01.lab.alcf.anl.gov**. This is required to point PBS simulator to the license server. 

### Basic Usage 
For its operation, the simulator needs atleast the following three flags to be set: (1) "--machine_name" (name of the ALCF system to be simulated), "--start_time" (start timestamp of simulation in YYYY-MM-DD HH:MM:SS), and "--end_time" (end timestamp of simulation in YYYY-MM-DD HH:MM:SS).
An example usage is:
```
python3 simsh.py --machine_name="polaris" --start_time="2023-02-10 19:00:00" --end_time="2023-02-14 19:00:00"
```

### Setting Backfill
If you want to set backfilling of jobs during scheduling, the "--backfill" flag needs to be set. Note that, this works only if the DIM_JOB_COMPOSITE has some kind of backfill queue. 
An example usage is:
```
python3 simsh.py --machine_name="polaris" --start_time="2023-02-10 19:00:00" --end_time="2023-02-14 19:00:00" --backfill
```
### Setting Queue Priority
By default, all queues have the same priority (i.e., 1). But, if you want to set higher priority of some queues with respect to others, it can be set using the "--set_priority" flag. 
An example usage is:
```
python3 simsh.py --machine_name="polaris" --start_time="2023-02-10 19:00:00" --end_time="2023-02-14 19:00:00" --set_priority="analysis:2, demand:4"
```
Here, "analysis" and "demand" queues are set priorities of 2 and 4, respectively (all others queues have a priority value of 1). This means that that jobs in the "analysis" queue is given four times more priority than jobs in other queues during scheduling. If the "--set_priority" flag sets priority of a queue which does not exist in the present simulation, it is ignored. 

### Reserving nodes for queues
By default, no nodes are reserved for any queues, i.e., all the nodes in the system are available for all the queues. But, this tool allows you to reserve nodes for certain queues which only jobs submitted to those specific queues can use, and no other queue can use such reserved node. This can be done by the "--set_reservation" flag. 
An example usage is:
```
python3 simsh.py --machine_name="polaris" --start_time="2023-02-10 19:00:00" --end_time="2023-02-14 19:00:00" --set_reservation="preemptable:56, analysis:10"
```

Here "preemptable" and "analysis" queues are reserved 56 and 10 nodes, respectively. If jobs submitted in these queues require more than those reserved nodes, they are not executed. If the "--set_reservation" flag sets reservation of a queue which does not exist in the present simulation, it is ignored. 

### Setting the job sort scheduling formula
This tool allows you to set the job sort formula to study how the scheduling changes with different sorting formula. *Any parameter from the DIM_JOB_COMPOSITE can be used in the job sort formula as long as it has a sortable numerical value.* This can be done using the "--select_sort_formula" flag.
An example usage is:
```
python3 simsh.py --machine_name="polaris" --start_time="2023-02-10 19:00:00" --end_time="2023-02-14 19:00:00" --select_sort_formula="QUEUED_TIMESTAMP:2, WALLTIME_SECONDS:1"
```
In this case the job sort formula is *2\*QUEUED_TIMESTAMP + 1\*WALLTIME_SECONDS*. If the flag is not set, by default, the job sort scheduling formula is *1\*QUEUED_TIMESTAMP + 1\*WALLTIME_SECONDS*.

### Using all flags together
The following example shows the siumlation using all the aforementioned flags together:
```
python3 simsh.py --machine_name="polaris" --start_time="2023-02-10 19:00:00" --end_time="2023-02-14 19:00:00" --backfill --set_priority="analysis:2, demand:4" --set_reservation="preemptable:56, analysis:10" --select_sort_formula="QUEUED_TIMESTAMP:2, WALLTIME_SECONDS:1"
```

### Optimizing the job sort scheduling formula
The "--optimize" flag outputs the job sort formula which optimizes thorughput. It brute-forces through all options to find the most optimal one. This tool will be soon updated to add optimizations for other metrics like utilization, fairness, etc. will be added. 
```
python3 simsh.py --machine_name="polaris" --start_time="2023-02-10 19:00:00" --end_time="2023-02-14 19:00:00" --optimize
```

### Output
Output of all print statements of *simsh.py* are stored in *output.log* file. It contains system information, when a particular job was scheduled, when it finished execution, how many nodes it ran on, if a running job is a backfill job, and which jobs did not finish execution with the simualation timeframe.
