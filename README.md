# PBS Global Scheduler
### Description of the tool
Here, the DIM_JOB_COMPOSITE logs of all ALCF systems (with PBS or non-PBS schedulers) are converted into pbs snapshots, which serve as an input to its simulator. These snapshots contain the accounting logs of the ALCF systems which is used by the simulator. This simulator allows users to try out different scheduling sorting formulas, use backfilling, and set different kinds of queue priorities and reservations. This will aid users to study scheduling strategies via ALCF job logs. 

### Installation and Setup Steps
1. Go to /soft/applications/
2. Download the PBSPro Simulation package: PBSPro\-sim\_2022.1.3\-/<OS/>/<OS version/>\_x86_64.tar.gz
3. tar xvfz PBSPro\-sim\_2022.1.3\-<OS><OS version>\_x86_64.tar.gz
4. cd PBSPro-sim_2022.1.0
5. In the sim.conf file, make sure to set the following: SIM_LICENSE_LOCATION=6200@<license server>
   In an ALCF system, you can use the followinf: SIM_LICENSE_LOCATION=6200@license-polaris-01.lab.alcf.anl.gov
6. Place the "simsh" directory inside /soft/applications/PBSPro-sim_2022.1.3/ 
7. cd /soft/applications/PBSPro-sim_2022.1.3/simsh
8. Run the simulation via: python3 simsh.py \<flag values\>

The "simsh" directory contains the converted snapshots with the accounting logs of all the ALCF systems, in the following path: /soft/applications/PBSPro-sim_2022.1.3/simsh/snap_\<ALCF system name\>. 
Inside each snapshot, the "data" folder contains the entire DIM_JOB_COMPOSITE data of the system. These are converted to accounting logs and placed at /soft/applications/PBSPro-sim_2022.1.3/simsh/snap_/<ALCF system name\>/server_priv/accounting

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
In this case the job sort formula is *2\*QUEUED_TIMESTAMP + 1\*WALLTIME_SECONDS*. Here, 2 and 1 are the weights given to QUEUED_TIMESTAMP and WALLTIME_SECONDS to create the job sorting formula. If a weight is positive, then a smaller value of the attribute is given priority, if it is negative then a larger value of the attribute is given priority in the sorting. If the flag is not set, by default, the job sort scheduling formula is *1\*QUEUED_TIMESTAMP + 1\*WALLTIME_SECONDS*.

### Using all flags together
The following example shows the siumlation using all the aforementioned flags together:
```
python3 simsh.py --machine_name="polaris" --start_time="2023-02-10 19:00:00" --end_time="2023-02-14 19:00:00" --backfill --set_priority="analysis:2, demand:4" --set_reservation="preemptable:56, analysis:10" --select_sort_formula="QUEUED_TIMESTAMP:2, WALLTIME_SECONDS:1"
```

### Output
Output of all print statements of *simsh.py* are stored in *output.log* file. It contains system information, when a particular job was scheduled, when it finished execution, how many nodes it ran on, if a running job is a backfill job, and which jobs did not finish execution with the simualation timeframe.
