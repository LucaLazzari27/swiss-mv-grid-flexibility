"""
Loads the prepared demand and DER profiles, applies optional PV, BESS, and
line-reinforcement configurations, and builds the network model input data.
"""
import json
import math
import numpy as np
import pandas as pd
from types import SimpleNamespace
from loader import Grid20EVBaselineLoader
import config

def get_timesteps():
    time_columns_df = pd.read_csv(config.FFOR_INPUT_DIR / "time_columns.csv")
    available_times = time_columns_df["time_column"].astype(str).tolist()

    if config.TARGET_TIME not in available_times:
        raise ValueError(f"TARGET_TIME {config.TARGET_TIME} not in available time columns")

    start_idx = available_times.index(config.TARGET_TIME)
    
    # State dynamics require the operating point at the end of the final interval.
    if start_idx + config.N_TIMESTEPS + 1 > len(available_times):
        raise ValueError(
            f"Not enough timesteps after {config.TARGET_TIME}: need {config.N_TIMESTEPS + 1}, "
            f"have {len(available_times) - start_idx}"
        )

    # Include both interval starts and the final state timepoint.
    return available_times[start_idx : start_idx + config.N_TIMESTEPS + 1]

TIMESTEPS = get_timesteps()
N_TIMEPOINTS = len(TIMESTEPS)

def load_data():
    loader = Grid20EVBaselineLoader(
        grid_name=config.GRID_NAME,
        data_year=config.DATA_YEAR,
        start_date="01-01 00:00:00",
        end_date="12-31 23:00:00",
        setpoint_c=config.T_SET_C,
    )
    loader.project_path = config.DATA_ROOT
    loader.geo_lv_path = config.DATA_ROOT / "06_Grids" / "geojson_data" / "LV"
    loader.load_grid()
    # The final delivery consumes the prepared FFOR input package directly,
    # without requiring the original multi-gigabyte raw demand dataset.
    loader.time_columns = pd.read_csv(
        config.FFOR_INPUT_DIR / "time_columns.csv"
    )["time_column"].astype(str).tolist()

    loader.baseline_demand_by_mv_node = pd.read_csv(config.PROFILE_DIR / "baseline_demand_by_mv_node.csv")
    loader.hp_baseline_by_mv_node = pd.read_csv(config.PROFILE_DIR / "hp_baseline_by_mv_node.csv")
    loader.pv_generation_by_mv_node = pd.read_csv(config.PROFILE_DIR / "pv_generation_by_mv_node.csv")
    loader.ev_baseline_by_mv_node = pd.read_csv(config.PROFILE_DIR / "ev_baseline_by_mv_node.csv")
    loader.ev_baseline_q_by_mv_node = pd.read_csv(config.PROFILE_DIR / "ev_baseline_q_by_mv_node.csv")
    loader.bess_allocation = pd.read_csv(config.PROFILE_DIR / "bess_allocation_mveq.csv")

    node_ids = [str(x) for x in loader.nodes["osmid"].astype(str)]
    bus_ids = [int(x) + 1 for x in node_ids]
    bus_to_pos = {bus: i for i, bus in enumerate(bus_ids)}
    osmid_to_bus = {osmid: int(osmid) + 1 for osmid in node_ids}
    n_bus = len(node_ids)

    slack_bus = int(pd.read_excel(config.DATA_ROOT / "06_Grids/20_0_matpower.xlsx", sheet_name="generator_data").iloc[0]["GEN_BUS"])
    slack_idx = bus_to_pos[slack_bus]

    # Fixed active and reactive demand
    def profile_by_node(df):
        out = df.copy()
        out["MV_osmid"] = out["MV_osmid"].astype(str)
        return out.set_index("MV_osmid")

    demand_df = profile_by_node(loader.baseline_demand_by_mv_node)
    ev_demand_df = profile_by_node(loader.ev_baseline_by_mv_node)
    ev_q_df = profile_by_node(loader.ev_baseline_q_by_mv_node)
    bus_data = pd.read_csv(config.DATA_ROOT / "06_Grids/20_0_bus_data.csv")
    bus_data["BUS_I"] = bus_data["BUS_I"].astype(int)
    q_over_p = {
        int(row.BUS_I): (float(row.Qd) / float(row.Pd) if abs(float(row.Pd)) > 1e-12 else 0.0)
        for _, row in bus_data.iterrows()
    }

    fixed_p_kW_t = np.zeros((n_bus, N_TIMEPOINTS))
    fixed_q_kvar_t = np.zeros((n_bus, N_TIMEPOINTS))

    for t, ts in enumerate(TIMESTEPS):
        conv_p = np.array([float(demand_df.loc[osmid, ts]) if osmid in demand_df.index else 0.0 for osmid in node_ids])
        ev_p = np.array([float(ev_demand_df.loc[osmid, ts]) if osmid in ev_demand_df.index else 0.0 for osmid in node_ids])
        conv_q = np.array([conv_p[i] * q_over_p.get(bus_ids[i], 0.0) for i in range(n_bus)])
        ev_q = np.array([float(ev_q_df.loc[osmid, ts]) if osmid in ev_q_df.index else 0.0 for osmid in node_ids])
        fixed_p_kW_t[:, t] = conv_p + ev_p
        fixed_q_kvar_t[:, t] = conv_q + ev_q

    # Distributed and grid-scale PV assets
    connected_lv = set(loader.mv_to_lv.values())
    pv_assets = []

    # Grid-scale PV must use a fixed normalization reference across every run.
    # Build full-year regional and bus-local shapes first, then slice TIMESTEPS.
    full_time_columns = pd.read_csv(config.FFOR_INPUT_DIR / "time_columns.csv")["time_column"].astype(str).tolist()
    full_time_index = {ts: i for i, ts in enumerate(full_time_columns)}
    window_positions = np.array([full_time_index[ts] for ts in TIMESTEPS], dtype=int)
    global_pv_full_shape = np.zeros(len(full_time_columns), dtype=float)
    local_pv_full_shapes = {}

    def full_pv_profile(row):
        return (
            pd.to_numeric(row.reindex(full_time_columns), errors="coerce")
            .fillna(0.0)
            .to_numpy(dtype=float)
        )

    def register_existing_pv(bus_id, profile, source):
        global_pv_full_shape[:] += profile
        if bus_id not in local_pv_full_shapes:
            local_pv_full_shapes[bus_id] = np.zeros(len(full_time_columns), dtype=float)
        local_pv_full_shapes[bus_id] += profile

        p_base_t = profile[window_positions]
        if p_base_t.max() > 1e-9:
            pv_assets.append({"bus": bus_id, "p_base_kW_t": p_base_t, "source": source})

    path = config.FFOR_INPUT_DIR / "pv_mv_generation.csv"
    if path.exists():
        mv_pv = pd.read_csv(path)
        for _, row in mv_pv.iterrows():
            mv_osmid = str(row["MV_osmid"])
            if mv_osmid not in osmid_to_bus:
                continue
            register_existing_pv(osmid_to_bus[mv_osmid], full_pv_profile(row), "MV")

    path = config.FFOR_INPUT_DIR / "pv_lv_generation.csv"
    if path.exists():
        lv_pv = pd.read_csv(path)
        lv_pv["LV_grid"] = lv_pv["LV_grid"].astype(str)
        lv_pv = lv_pv[lv_pv["LV_grid"].isin(connected_lv)].copy()
        for _, row in lv_pv.iterrows():
            mv_osmid = loader.lv_to_mv.get(str(row["LV_grid"]))
            if mv_osmid not in osmid_to_bus:
                continue
            register_existing_pv(osmid_to_bus[mv_osmid], full_pv_profile(row), "LV")

    # Grid-scale PV uses the local annual shape when available and the
    # aggregate regional shape otherwise.
    if hasattr(config, "GRID_SCALE_PV") and len(config.GRID_SCALE_PV) > 0:
        global_annual_peak = global_pv_full_shape.max()

        for pv in config.GRID_SCALE_PV:
            bus_id = pv["bus"]
            if bus_id not in bus_to_pos:
                continue

            local_full_shape = local_pv_full_shapes.get(bus_id)
            if local_full_shape is not None and local_full_shape.max() > 1e-9:
                chosen_shape = local_full_shape[window_positions] / local_full_shape.max()
            elif global_annual_peak > 1e-9:
                chosen_shape = global_pv_full_shape[window_positions] / global_annual_peak
            else:
                chosen_shape = np.zeros(N_TIMEPOINTS, dtype=float)

            p_base_t = np.clip(chosen_shape, 0.0, 1.0) * float(pv["p_peak_kW"])
            if p_base_t.max() > 1e-9:
                pv_assets.append({
                    "bus": bus_id,
                    "p_base_kW_t": p_base_t,
                    "source": "GRID_SCALE",
                })

    # Heat-pump assets and thermal parameters
    temperature_profiles = pd.read_csv(config.FFOR_INPUT_DIR / "temperature_profiles.csv").set_index("Temperature_profile_name")
    hp_assets = []
    
    def _float_or_default(value, default):
        try:
            out = float(value)
            return out if np.isfinite(out) else default
        except: return default

    def _hp_cop_for_row(row, t_out_array):
        delta = config.T_SET_C - np.asarray(t_out_array, dtype=float)
        if {"COP_0", "COP_1", "COP_2"}.issubset(row.index) and not pd.isna(row.get("COP_0")):
            cop = float(row["COP_0"]) + float(row["COP_1"]) * delta + float(row["COP_2"]) * delta * delta
        else: cop = np.full(len(delta), _float_or_default(row.get("COP", 3.0), 3.0), dtype=float)
        return np.maximum(cop, 1.0)

    HP_PEAK_CANDIDATE_COLUMNS = ["hp_p_peak_kW", "p_peak_kW", "P_peak_kW", "P_HP_peak_kW", "hp_P_peak_kW"]
    for path, source in [(config.FFOR_INPUT_DIR / "hp_mv_allocation.csv", "MV"), (config.FFOR_INPUT_DIR / "hp_lv_allocation.csv", "LV")]:
        if not path.exists(): continue
        hp = pd.read_csv(path)
        if source == "LV":
            hp["LV_grid"] = hp["LV_grid"].astype(str)
            hp = hp[hp["LV_grid"].isin(connected_lv)].copy()

        for _, row in hp.iterrows():
            mv_osmid = str(row["MV_osmid"]) if source == "MV" else loader.lv_to_mv.get(str(row["LV_grid"]))
            if mv_osmid not in osmid_to_bus: continue

            profile = loader._hp_consumption_for_row(row, temperature_profiles)
            p_base_t = np.array([float(profile[ts]) for ts in TIMESTEPS], dtype=float)

            hp_p_peak_kW = np.nan
            for col in HP_PEAK_CANDIDATE_COLUMNS:
                if col in row.index and not pd.isna(row.get(col)):
                    val = _float_or_default(row.get(col), np.nan)
                    if np.isfinite(val): hp_p_peak_kW = val; break
            
            # Use the profile maximum when no explicit peak-power field is available.
            if not np.isfinite(hp_p_peak_kW): hp_p_peak_kW = float(np.nanmax(profile)) if len(profile) else np.nan

            hp_p_max_flex_kW = hp_p_peak_kW / config.HP_POWER_MARGIN_FACTOR if np.isfinite(hp_p_peak_kW) else np.nan
            if not np.isfinite(hp_p_max_flex_kW) or hp_p_max_flex_kW <= 1e-9: continue

            profile_name = row.get("Temperature_profile_name", "")
            t_out_t = np.array([float(temperature_profiles.loc[profile_name, ts]) for ts in TIMESTEPS], dtype=float) if profile_name in temperature_profiles.index else np.full(N_TIMEPOINTS, config.T_SET_C, dtype=float)
            cop_t = _hp_cop_for_row(row, t_out_t)
            thermal_c = _float_or_default(row.get("Thermal_capacitance_KWh/K", np.nan), np.nan)
            thermal_h = _float_or_default(row.get("Thermal_conductivity_kW/K", np.nan), np.nan)
            has_thermal = bool(np.isfinite(thermal_c) and np.isfinite(thermal_h) and thermal_c > 0.0 and thermal_h > 0.0)

            hp_assets.append({
                "bus": osmid_to_bus[mv_osmid], "p_base_kW_t": p_base_t, "hp_p_max_flex_kW": hp_p_max_flex_kW,
                "t_out_C_t": t_out_t, "cop_t": cop_t, "thermal_capacitance_kWh_per_K": thermal_c,
                "thermal_conductivity_kW_per_K": thermal_h, "has_thermal_dynamics": has_thermal,
            })

    # Distributed BESS assets
    bess_assets = []
    for _, row in loader.bess_allocation.iterrows():
        mv_osmid = str(row["MV_osmid"])
        if mv_osmid in osmid_to_bus:
            bess_assets.append({
                "bus": osmid_to_bus[mv_osmid], 
                "capacity_kWh": float(row["Battery_capacity_kWh"]),
                "p_nom_kW": float(row["Nominal_power_kW"]), 
                "eta_ch": float(row.get("Charging_efficiency", 0.922)),
                "eta_dis": float(row.get("Discharging_efficiency", 0.922)),
                "soc_min": config.SOC_MIN,
                "soc_max": config.SOC_MAX,
                "soc_initial": config.SOC_INITIAL
            })  # Efficiencies are retained as data but neglected by the optimization.

    # Optional grid-scale BESS assets
    if hasattr(config, 'GRID_SCALE_BESS'):
        for bess in config.GRID_SCALE_BESS:
            bus_id = bess["bus"]
            if bus_id in bus_to_pos:
                bess_assets.append({
                    "bus": bus_id, 
                    "capacity_kWh": float(bess["capacity_kWh"]),
                    "p_nom_kW": float(bess["p_nom_kW"]), 
                    "eta_ch": 1.0,  # The optimization uses lossless state dynamics.
                    "eta_dis": 1.0,
                    "is_grid_scale": bess.get("is_grid_scale", True),
                    "soc_min": float(bess.get("soc_min", config.GRID_SOC_MIN)),
                    "soc_max": float(bess.get("soc_max", config.GRID_SOC_MAX)),
                    "soc_initial": float(bess.get("soc_initial", config.GRID_SOC_INITIAL))
                })
        

    # LINE PARAMETERS AND OPTIONAL GRID REINFORCEMENT
    branch = pd.read_csv(config.DATA_ROOT / "06_Grids/20_0_branch_data.csv")
    branch = branch[branch["BR_STATUS"].astype(int).eq(1)].copy()

    with open(config.DATA_ROOT / "06_Grids/20_0_edges.txt", "r") as f:
        edges_geojson = json.load(f)

    line_s_nom_mva = {}
    for feature in edges_geojson["features"]:
        props = feature["properties"]
        f_bus = int(props["u"]) + 1
        t_bus = int(props["v"]) + 1
        s_nom = float(props.get("s_nom", 0.0) or 0.0)

        line_s_nom_mva[(f_bus, t_bus)] = s_nom
        line_s_nom_mva[(t_bus, f_bus)] = s_nom

    reinforced_lines = {
        tuple(sorted((int(f_bus), int(t_bus))))
        for f_bus, t_bus in getattr(config, "REINFORCED_PHYSICAL_LINES", [])
    }

    pp_lines = pd.read_excel(
        config.DATA_ROOT / "06_Grids/20_0_grid.xlsx",
        sheet_name="line",
    )

    # Pandapower line endpoints use zero-based dataframe positions.
    bus_i_to_pp_index = {
        int(row["BUS_I"]): idx
        for idx, row in bus_data.reset_index(drop=True).iterrows()
    }

    reinforcement_records = []

    def physical_line_key(f_bus, t_bus):
        return tuple(sorted((int(f_bus), int(t_bus))))

    def matching_pp_line(f_bus, t_bus):
        if f_bus not in bus_i_to_pp_index or t_bus not in bus_i_to_pp_index:
            raise ValueError(
                f"Cannot map MATPOWER line {f_bus}-{t_bus} "
                "to the pandapower line table."
            )

        pp_f_bus = bus_i_to_pp_index[f_bus]
        pp_t_bus = bus_i_to_pp_index[t_bus]

        matches = pp_lines[
            (
                (pp_lines["from_bus"].astype(int) == pp_f_bus)
                & (pp_lines["to_bus"].astype(int) == pp_t_bus)
            )
            |
            (
                (pp_lines["from_bus"].astype(int) == pp_t_bus)
                & (pp_lines["to_bus"].astype(int) == pp_f_bus)
            )
        ]

        if len(matches) != 1:
            raise ValueError(
                f"Expected exactly one pandapower line for "
                f"MATPOWER line {f_bus}-{t_bus}, found {len(matches)}."
            )

        return matches.iloc[0]

    def line_electrical_parameters(row):
        f_bus = int(row["F_BUS"])
        t_bus = int(row["T_BUS"])
        key = physical_line_key(f_bus, t_bus)

        old_r = float(row["BR_R"])
        old_x = float(row["BR_X"])
        old_b = float(row["BR_B"])
        old_s_nom = float(line_s_nom_mva[(f_bus, t_bus)])

        if key not in reinforced_lines:
            return old_r, old_x, old_b, old_s_nom

        pp_line = matching_pp_line(f_bus, t_bus)
        line_type = str(pp_line.get("type", "")).strip().lower()
        length_km = float(pp_line["length_km"])

        base_kv = float(
            bus_data.loc[
                bus_data["BUS_I"].eq(f_bus),
                "baseKV",
            ].iloc[0]
        )
        z_base_ohm = base_kv**2 / config.SBASE_MVA

        if line_type in {"ol", "ohl", "overhead", "overhead_line"}:
            reinforcement_type = "overhead_line"
            parameters = config.OVERHEAD_REINFORCEMENT

            new_r = (
                parameters["r_ohm_per_km"]
                * length_km
                / z_base_ohm
            )
            new_x = (
                parameters["x_ohm_per_km"]
                * length_km
                / z_base_ohm
            )

            # Maintain the original overhead-line shunt susceptance.
            new_b = old_b
            new_s_nom = parameters["s_nom_mva"]

        elif line_type in {"cs", "cable", "cables", "ug", "underground"}:
            reinforcement_type = "cable"
            parameters = config.CABLE_REINFORCEMENT

            new_r = (
                parameters["r_ohm_per_km"]
                * length_km
                / z_base_ohm
            )
            new_x = (
                parameters["x_ohm_per_km"]
                * length_km
                / z_base_ohm
            )

            new_b = (
                2.0
                * math.pi
                * config.REINFORCEMENT_F_HZ
                * parameters["c_nf_per_km"]
                * 1e-9
                * length_km
                * z_base_ohm
            )
            new_s_nom = parameters["s_nom_mva"]

        else:
            raise ValueError(
                f"Unsupported line type {line_type!r} for reinforced "
                f"line {f_bus}-{t_bus}."
            )

        line_s_nom_mva[(f_bus, t_bus)] = new_s_nom
        line_s_nom_mva[(t_bus, f_bus)] = new_s_nom

        reinforcement_records.append({
            "physical_line": f"{key[0]}-{key[1]}",
            "from_bus": f_bus,
            "to_bus": t_bus,
            "reinforcement_type": reinforcement_type,
            "length_km": length_km,
            "old_BR_R_pu": old_r,
            "old_BR_X_pu": old_x,
            "old_BR_B_pu": old_b,
            "old_s_nom_mva": old_s_nom,
            "new_BR_R_pu": new_r,
            "new_BR_X_pu": new_x,
            "new_BR_B_pu": new_b,
            "new_s_nom_mva": new_s_nom,
        })

        return new_r, new_x, new_b, new_s_nom

    Jp_theta = np.zeros((n_bus, n_bus))
    Jp_u = np.zeros((n_bus, n_bus))
    Jq_u = np.zeros((n_bus, n_bus))
    p_ref = np.zeros(n_bus)
    q_ref = np.zeros(n_bus)
    line_params = []

    for _, row in branch.iterrows():
        f_bus = int(row["F_BUS"])
        t_bus = int(row["T_BUS"])

        if f_bus not in bus_to_pos or t_bus not in bus_to_pos:
            continue

        i = bus_to_pos[f_bus]
        k = bus_to_pos[t_bus]

        r, x, b_total, s_nom_mva = line_electrical_parameters(row)

        y = 1 / complex(r, x)
        g = y.real
        b = y.imag
        b_sh = b_total / 2.0

        Jp_theta[i, i] += -b
        Jp_theta[k, k] += -b
        Jp_theta[i, k] += b
        Jp_theta[k, i] += b

        Jp_u[i, i] += g
        Jp_u[k, k] += g
        Jp_u[i, k] += -g
        Jp_u[k, i] += -g

        Jq_u[i, i] += -2.0 * b_sh - b
        Jq_u[k, k] += -2.0 * b_sh - b
        Jq_u[i, k] += b
        Jq_u[k, i] += b

        q_ref[i] += -b_sh
        q_ref[k] += -b_sh

        line_params.append({
            "from_bus": f_bus,
            "to_bus": t_bus,
            "i": i,
            "k": k,
            "g": g,
            "b": b,
            "b_sh": b_sh,
            "s_nom_mva": s_nom_mva,
            "is_reinforced": physical_line_key(f_bus, t_bus)
            in reinforced_lines,
        })

    reinforcement_lines_df = pd.DataFrame(reinforcement_records)

    expected_lines = {
        f"{line[0]}-{line[1]}"
        for line in reinforced_lines
    }

    found_lines = (
        set(reinforcement_lines_df["physical_line"])
        if not reinforcement_lines_df.empty
        else set()
    )

    if found_lines != expected_lines:
        raise RuntimeError(
            "Reinforcement line mismatch. "
            f"Missing={sorted(expected_lines - found_lines)}, "
            f"unexpected={sorted(found_lines - expected_lines)}"
        )

    return SimpleNamespace(
        TIMESTEPS=TIMESTEPS, bus_ids=bus_ids, n_bus=n_bus, slack_idx=slack_idx, slack_bus=slack_bus,
        fixed_p_kW_t=fixed_p_kW_t, fixed_q_kvar_t=fixed_q_kvar_t, pv_assets=pv_assets, hp_assets=hp_assets,
        bess_assets=bess_assets, line_params=line_params, Jp_theta=Jp_theta, Jp_u=Jp_u, Jq_theta=-Jp_u, Jq_u=Jq_u, 
        p_ref=p_ref, q_ref=q_ref, reinforcement_lines_df=reinforcement_lines_df,
    )
