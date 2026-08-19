# Parallel Simulation & Job Management

## Table of Contents
- Batch with run_parallel()
- Incremental submission with SpectrePool
- Multi-server simulation
- .env configuration

---

## Batch with `run_parallel()`

Submit all at once and wait for completion. Each call creates and releases its
own executor, so repeated batches may safely use different concurrency limits:

```python
results = sim.run_parallel([
    (Path("tb_comp.scs"), {"include_files": ["comp.va"]}),
    (Path("tb_dac.scs"), {}),
    (Path("tb_logic.scs"), {}),
], max_workers=5)
```

`max_workers` is the number of independent Spectre processes, not the thread
count used inside one Spectre process. The conservative default is 4; set it
explicitly according to license, CPU, memory, and SSH session limits.

## Incremental submission with `SpectrePool`

When tasks arrive over time, create an explicitly owned pool. `submit()`
returns a `Future` immediately, and leaving the context waits for outstanding
work and releases the executor:

```python
sim = SpectreSimulator.from_env()

with sim.parallel_pool(max_workers=4) as pool:
    t1 = pool.submit(Path("tb_comparator.scs"), {"include_files": ["comp.va"]})
    t2 = pool.submit(Path("tb_dac.scs"))

    if t1.done():
        result = t1.result()

    t3 = pool.submit(Path("tb_sar_logic.scs"))
    results = pool.wait_all([t1, t2, t3])
```

Each task gets a unique `<netlist-stem>__<run-id>/` directory below the
configured local `work_dir`, as well as its own remote directory when
applicable. This isolates PSF data, logs, initial-condition files, and other
auxiliary files even when the same deck is submitted more than once. The
synchronous `run_simulation()` path continues to use `work_dir` directly.
With `VB_SSH_BACKEND=paramiko`, all tasks in one simulator share one
authenticated SSH Transport. `VB_SSH_MAX_SESSIONS` bounds its concurrent
channels and must not exceed the target sshd `MaxSessions`; additional tasks
wait for a permit. The backend does not fall back to one TCP connection per
task.

## Multi-server simulation

Create a simulator per profile to distribute work across machines:

```python
# .env defines VB_REMOTE_HOST_worker1, VB_REMOTE_HOST_worker2, etc.
sim1 = SpectreSimulator(remote=True, profile="worker1")
sim2 = SpectreSimulator(remote=True, profile="worker2")

with sim1.parallel_pool(max_workers=2) as pool1, \
     sim2.parallel_pool(max_workers=2) as pool2:
    t1 = pool1.submit(Path("tb_comp.scs"))
    t2 = pool2.submit(Path("tb_dac.scs"))

    results = SpectreSimulator.wait_all([t1, t2])
```

## .env configuration

The `.env` file can live in the virtuoso-bridge-lite directory or in your project root
(recommended when virtuoso-bridge-lite is cloned as a subdirectory). Both locations
are searched automatically.

```dotenv
# Default connection
VB_REMOTE_HOST=my-server
VB_REMOTE_USER=username
VB_REMOTE_PORT=65081
VB_LOCAL_PORT=65082
VB_CADENCE_CSHRC=/path/to/.cshrc.cadence
VB_SSH_BACKEND=paramiko
VB_SSH_MAX_SESSIONS=10

# Additional profiles for multi-server
VB_REMOTE_HOST_worker1=eda-node1
VB_REMOTE_USER_worker1=sim_user
VB_REMOTE_PORT_worker1=65432
VB_LOCAL_PORT_worker1=65433
```
