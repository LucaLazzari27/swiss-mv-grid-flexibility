# Optimization in Energy Systems Project

**Authors:** Pierpaolo Musiello, Luca Lazzari, and Alessandro Falezza

This folder contains the final multistep FFOR formulation and the prepared
input data required to run it independently.

**Node-numbering convention:** the configuration uses internal grid-data bus
IDs, while the report labels each node one number lower. For example, code bus
`130` corresponds to report node `N129`.

## Files

- `config.py`: scenario, DER, BESS, and reinforcement configuration.
- `loader.py`: grid and profile loading utilities.
- `model_data.py`: prepared-profile processing and optimization-data assembly.
- `optimization.py`: linear multistep FFOR optimization model.
- `run.py`: QuickFlex boundary calculation and output generation.
- `requirements.txt`: required Python packages.
- `data_checksums_sha256.csv`: optional integrity check for the prepared data.

## Quick start

1. Install the packages listed in `requirements.txt`.
2. Ensure that a valid Gurobi license is available.
3. Select the scenario and optional assets in `config.py`.
4. Run the model from this directory:

```powershell
python run.py
```

Each run writes:

- `ffor_boundary.csv`
- `ffor_boundary.png`

directly into this folder, replacing files from the previous run.

By default, the code also looks for data inside this folder. To keep the data
elsewhere, set `FFOR_DATA_ROOT` to the directory containing `06_Grids/` and
`outputs/` before running the model. This supports read-only, authorized, or
cloud-synchronized data locations without editing the Python modules. The year,
target time, timestep count, and timestep duration can also be set through the
`FFOR_DATA_YEAR`, `FFOR_TARGET_TIME`, `FFOR_N_TIMESTEPS`, and `FFOR_DT_H`
environment variables.

## Required data layout

Run the code from this folder and place the prepared data underneath it using
the following structure:

```text
final_delivery/
|-- 06_Grids/
|   |-- 20_0_nodes.txt
|   |-- 20_0_edges.txt
|   |-- 20_0_matpower.xlsx
|   |-- 20_0_bus_data.csv
|   |-- 20_0_branch_data.csv
|   `-- 20_0_grid.xlsx
`-- outputs/
    |-- 20_0_profiles_mveq_evbase_2030/
    |-- 20_0_profiles_mveq_evbase_2040/
    `-- 20_0_profiles_mveq_evbase_2050/
```

Each yearly profile directory requires these files:

```text
baseline_demand_by_mv_node.csv
hp_baseline_by_mv_node.csv
pv_generation_by_mv_node.csv
ev_baseline_by_mv_node.csv
ev_baseline_q_by_mv_node.csv
bess_allocation_mveq.csv
ffor_inputs/time_columns.csv
ffor_inputs/temperature_profiles.csv
ffor_inputs/hp_mv_allocation.csv
ffor_inputs/hp_lv_allocation.csv
ffor_inputs/pv_mv_generation.csv
ffor_inputs/pv_lv_generation.csv
```

## Optional configurations

The following studies can be configured directly in `config.py`:

- additional grid-scale PV installations through `GRID_SCALE_PV`;
- additional grid-scale BESS installations through `GRID_SCALE_BESS`;
- physical-line reinforcement through `REINFORCED_PHYSICAL_LINES`.

Leave these lists empty to simulate the original grid without additional
grid-scale assets or reinforcement.

## Data integrity

`data_checksums_sha256.csv` records the expected SHA-256 hash of each prepared
data file. It is not required by the simulation, but should be retained when
the data package is transferred or downloaded so recipients can verify that
the inputs are complete and unchanged.
