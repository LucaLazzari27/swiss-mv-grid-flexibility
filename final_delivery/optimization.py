"""
Builds the multistep linear FFOR model, including network balances, DER
capability constraints, inter-temporal states, and the Gurobi solve routine.
"""
import numpy as np
import math
import gurobipy as gp
from gurobipy import GRB
from types import SimpleNamespace
import config

def build_optimization_model(data):
    # Variable indexing and bounds
    idx = {}; bounds = []; var_names = []

    def add_var(name, lb=None, ub=None):
        j = len(bounds)
        idx[name] = j
        var_names.append(name)
        bounds.append((lb, ub))
        return j

    # Sustained intervals require one additional timepoint for final states.
    N_TIMEPOINTS = config.N_TIMESTEPS + 1

    # Linearized network-state variables
    theta = {}; du = {}
    for t in range(N_TIMEPOINTS):
        for bus in data.bus_ids:
            theta[(bus, t)] = add_var(f"theta_{bus}_t{t}", None, None)
            du[(bus, t)] = add_var(f"du_{bus}_t{t}", -0.05, 0.05)

    # PCC exchange and common sustained-flexibility variables
    pcc_p = {}; pcc_q = {}
    for t in range(N_TIMEPOINTS):
        pcc_p[t] = add_var(f"pcc_p_pu_t{t}", None, None)
        pcc_q[t] = add_var(f"pcc_q_pu_t{t}", None, None)

    p_flex_common = add_var("P_flex_common_pu", None, None)
    q_flex_common = add_var("Q_flex_common_pu", None, None)

    # Controllable DER dispatch variables
    pv_p = {}; pv_q = {}
    for a, asset in enumerate(data.pv_assets):
        for t in range(N_TIMEPOINTS):
            p_avail = asset["p_base_kW_t"][t]
            pv_p[(a, t)] = add_var(f"pv_p_{a}_t{t}", 0.0, p_avail)
            pv_q[(a, t)] = add_var(f"pv_q_{a}_t{t}", -config.TAN_PHI * p_avail, config.TAN_PHI * p_avail)

    hp_p_el = {}
    for a, asset in enumerate(data.hp_assets):
        for t in range(N_TIMEPOINTS):
            baseline_p = asset.get("p_base_kW_t", [0.0]*N_TIMEPOINTS)[t]
            if baseline_p <= 1e-4:  
                max_p = 0.0
            else:
                max_p = asset["hp_p_max_flex_kW"]
            hp_p_el[(a, t)] = add_var(f"hp_p_el_{a}_t{t}", 0.0, max_p)

    bess_p = {}; bess_q = {}
    for a, asset in enumerate(data.bess_assets):
        for t in range(N_TIMEPOINTS):
            p_nom = asset["p_nom_kW"]
            bess_p[(a, t)] = add_var(f"bess_p_{a}_t{t}", -p_nom, p_nom)
            bess_q[(a, t)] = add_var(f"bess_q_{a}_t{t}", -p_nom, p_nom)

    # Inter-temporal state variables
    bess_soc = {}; hp_temp = {}
    for a, asset in enumerate(data.bess_assets):
        for t in range(N_TIMEPOINTS):
            bess_soc[(a, t)] = add_var(f"bess_soc_{a}_t{t}", asset["soc_min"], asset["soc_max"])
    for a in range(len(data.hp_assets)):
        for t in range(N_TIMEPOINTS):
            hp_temp[(a, t)] = add_var(f"hp_temp_{a}_t{t}", config.T_MIN_C, config.T_MAX_C)

    # Sparse linear-constraint storage
    eq_constraints = []; b_eq = []; eq_names = []
    ub_constraints = []; b_ub = []; ub_names = []

    def _clean_coefs(coefs, tol=1e-14):
        cleaned = {}
        for j, value in coefs.items():
            if value is None or abs(float(value)) <= tol: continue
            cleaned[int(j)] = cleaned.get(int(j), 0.0) + float(value)
        return {j: v for j, v in cleaned.items() if abs(v) > tol}

    def add_eq(coefs, rhs, name="unnamed"):
        eq_constraints.append(_clean_coefs(coefs)); b_eq.append(float(rhs)); eq_names.append(name)

    def add_ub(coefs, rhs, name="unnamed"):
        ub_constraints.append(_clean_coefs(coefs)); b_ub.append(float(rhs)); ub_names.append(name)

    # Asset-to-bus lookup and polygon normals
    pv_by_bus = {bus: [] for bus in data.bus_ids}; hp_by_bus = {bus: [] for bus in data.bus_ids}; bess_by_bus = {bus: [] for bus in data.bus_ids}
    for a, asset in enumerate(data.pv_assets): pv_by_bus[asset["bus"]].append(a)
    for a, asset in enumerate(data.hp_assets): hp_by_bus[asset["bus"]].append(a)
    for a, asset in enumerate(data.bess_assets): bess_by_bus[asset["bus"]].append(a)

    normals = [(math.cos(2.0 * math.pi * j / config.N_POLY + math.pi / config.N_POLY), math.sin(2.0 * math.pi * j / config.N_POLY + math.pi / config.N_POLY)) for j in range(config.N_POLY)]

    # Per-timepoint network and device constraints
    for t in range(N_TIMEPOINTS):
        add_eq({theta[(data.slack_bus, t)]: 1.0}, 0.0, name=f"slack_theta_t{t}")
        add_eq({du[(data.slack_bus, t)]: 1.0}, 0.0, name=f"slack_du_t{t}")

        # Linearized active- and reactive-power balance at every bus
        for i, bus in enumerate(data.bus_ids):
            coefs_p = {pcc_p[t]: 1.0} if i == data.slack_idx else {}
            for a in pv_by_bus[bus]: coefs_p[pv_p[(a, t)]] = coefs_p.get(pv_p[(a, t)], 0.0) + 1.0 / config.SBASE_KW
            for a in hp_by_bus[bus]: coefs_p[hp_p_el[(a, t)]] = coefs_p.get(hp_p_el[(a, t)], 0.0) - 1.0 / config.SBASE_KW
            for a in bess_by_bus[bus]: coefs_p[bess_p[(a, t)]] = coefs_p.get(bess_p[(a, t)], 0.0) + 1.0 / config.SBASE_KW
            for k in range(data.n_bus):
                if abs(data.Jp_theta[i, k]) > 0.0: coefs_p[theta[(data.bus_ids[k], t)]] = coefs_p.get(theta[(data.bus_ids[k], t)], 0.0) - data.Jp_theta[i, k]
                if abs(data.Jp_u[i, k]) > 0.0: coefs_p[du[(data.bus_ids[k], t)]] = coefs_p.get(du[(data.bus_ids[k], t)], 0.0) - data.Jp_u[i, k]
            add_eq(coefs_p, data.p_ref[i] + data.fixed_p_kW_t[i, t] / config.SBASE_KW, name=f"P_balance_bus{bus}_t{t}")

            coefs_q = {pcc_q[t]: 1.0} if i == data.slack_idx else {}
            for a in pv_by_bus[bus]: coefs_q[pv_q[(a, t)]] = coefs_q.get(pv_q[(a, t)], 0.0) + 1.0 / config.SBASE_KVAR
            for a in hp_by_bus[bus]: coefs_q[hp_p_el[(a, t)]] = coefs_q.get(hp_p_el[(a, t)], 0.0) - config.TAN_PHI / config.SBASE_KVAR
            for a in bess_by_bus[bus]: coefs_q[bess_q[(a, t)]] = coefs_q.get(bess_q[(a, t)], 0.0) + 1.0 / config.SBASE_KVAR
            for k in range(data.n_bus):
                if abs(data.Jq_theta[i, k]) > 0.0: coefs_q[theta[(data.bus_ids[k], t)]] = coefs_q.get(theta[(data.bus_ids[k], t)], 0.0) - data.Jq_theta[i, k]
                if abs(data.Jq_u[i, k]) > 0.0: coefs_q[du[(data.bus_ids[k], t)]] = coefs_q.get(du[(data.bus_ids[k], t)], 0.0) - data.Jq_u[i, k]
            add_eq(coefs_q, data.q_ref[i] + data.fixed_q_kvar_t[i, t] / config.SBASE_KVAR, name=f"Q_balance_bus{bus}_t{t}")

        # Triangular constant-power-factor PV capability
        for a in range(len(data.pv_assets)):
            add_ub({pv_q[(a, t)]: 1.0, pv_p[(a, t)]: -config.TAN_PHI}, 0.0, name=f"Limite_Q+_PV_{a}_t{t}")
            add_ub({pv_q[(a, t)]: -1.0, pv_p[(a, t)]: -config.TAN_PHI}, 0.0, name=f"Limite_Q-_PV_{a}_t{t}")

        # Polygonal BESS inverter capability
        for a, asset in enumerate(data.bess_assets):
            p_nom = asset["p_nom_kW"]
            for n_id, (n_p, n_q) in enumerate(normals):
                add_ub({bess_p[(a, t)]: n_p, bess_q[(a, t)]: n_q}, p_nom * config.INNER_POLY_RADIUS, name=f"Lim_S_BESS_{a}_t{t}_{n_id}")

        # Polygonal apparent-power limits at both ends of every line
        for prm in data.line_params:
            s_lim = prm["s_nom_mva"] / config.SBASE_MVA
            for side_i, side_k in [(prm["i"], prm["k"]), (prm["k"], prm["i"])]:
                i_bus, k_bus = data.bus_ids[side_i], data.bus_ids[side_k]
                for n_id, (n_p, n_q) in enumerate(normals):
                    add_ub({
                        theta[(i_bus, t)]: -n_p * prm["b"] - n_q * prm["g"], theta[(k_bus, t)]: n_p * prm["b"] + n_q * prm["g"],
                        du[(i_bus, t)]: n_p * prm["g"] - n_q * (2.0 * prm["b_sh"] + prm["b"]), du[(k_bus, t)]: -n_p * prm["g"] + n_q * prm["b"],
                    }, s_lim * config.INNER_POLY_RADIUS + n_q * prm["b_sh"], name=f"Line_{i_bus}_to_{k_bus}_t{t}_{n_id}")

    # BESS state-of-charge dynamics and optional terminal condition
    for a, asset in enumerate(data.bess_assets): 
        add_eq({bess_soc[(a, 0)]: 1.0}, asset["soc_initial"])
        
    for a, asset in enumerate(data.bess_assets):
        cap = asset["capacity_kWh"]
        for t in range(config.N_TIMESTEPS):
            
            add_eq({
                bess_soc[(a, t + 1)]: 1.0, 
                bess_soc[(a, t)]: -1.0, 
                bess_p[(a, t)]: config.DT_H / cap,
            }, 0.0)
    
    if config.ENFORCE_FINAL_SOC:
        for a, asset in enumerate(data.bess_assets):
            if asset.get("is_grid_scale", False) == True: continue 
            add_ub({bess_soc[(a, config.N_TIMESTEPS)]: -1.0}, -(asset["soc_initial"] - config.SOC_FINAL_TOL), name=f"SOC_final_min_{a}")
            add_ub({bess_soc[(a, config.N_TIMESTEPS)]: 1.0}, asset["soc_initial"] + config.SOC_FINAL_TOL, name=f"SOC_final_max_{a}")

    # Heat-pump thermal dynamics and optional terminal condition
    for a in range(len(data.hp_assets)): add_eq({hp_temp[(a, 0)]: 1.0}, config.T_SET_C)
    
    for a, asset in enumerate(data.hp_assets):
        if asset["has_thermal_dynamics"]:
            C, H = asset["thermal_capacitance_kWh_per_K"], asset["thermal_conductivity_kW_per_K"]
            for t in range(config.N_TIMESTEPS):
                # Explicit-Euler first-order thermal state update
                add_eq({
                    hp_temp[(a, t + 1)]: 1.0, 
                    hp_temp[(a, t)]: -(1.0 - config.DT_H * H / C),
                    hp_p_el[(a, t)]: -(config.DT_H * asset["cop_t"][t] / C),
                }, (config.DT_H * H / C) * asset["t_out_C_t"][t])
        else:
            for t in range(config.N_TIMESTEPS): add_eq({hp_temp[(a, t + 1)]: 1.0, hp_temp[(a, t)]: -1.0}, 0.0)

    if config.ENFORCE_FINAL_TEMPERATURE:
        for a in range(len(data.hp_assets)): add_ub({hp_temp[(a, config.N_TIMESTEPS)]: -1.0}, -(config.T_SET_C - config.T_FINAL_TOL))

    # Gurobi model construction, solution, and active-constraint reporting
    def solve_with_bounds(c, local_bounds, model_name="ffor_multistep_lp", include_ub=True):
        model = gp.Model(model_name); model.Params.OutputFlag = 0
        x_vars = []
        for j, (lb, ub) in enumerate(local_bounds):
            x_vars.append(model.addVar(
                lb=-GRB.INFINITY if lb is None else float(lb), 
                ub=GRB.INFINITY if ub is None else float(ub), 
                vtype=GRB.CONTINUOUS, name=var_names[j]
            ))
        model.update()

        for row_id, (coefs, rhs) in enumerate(zip(eq_constraints, b_eq)):
            model.addConstr(gp.quicksum(float(v) * x_vars[int(j)] for j, v in coefs.items()) == float(rhs))
            
        gurobi_ub_constrs = []
        if include_ub:
            for row_id, (coefs, rhs) in enumerate(zip(ub_constraints, b_ub)):
                constr = model.addConstr(
                    gp.quicksum(float(v) * x_vars[int(j)] for j, v in coefs.items()) <= float(rhs), 
                    name=ub_names[row_id]
                )
                gurobi_ub_constrs.append(constr)

        # Apply the directional linear objective supplied by the QuickFlex search.
        obj_nz = np.flatnonzero(np.abs(c) > 1e-12)
        model.setObjective(gp.quicksum(float(c[j]) * x_vars[j] for j in obj_nz), GRB.MINIMIZE)
        model.optimize()

        if model.Status == GRB.OPTIMAL:
            binding_list = []
            for constr in gurobi_ub_constrs:
                if abs(constr.Slack) < 1e-5:
                    binding_list.append(constr.ConstrName)
            
            for j, var in enumerate(x_vars):
                if abs(var.UB - var.LB) < 1e-5: continue 
                if var.UB < GRB.INFINITY and abs(var.X - var.UB) < 1e-5:
                    binding_list.append(f"{var_names[j]}_at_MAX")
                elif var.LB > -GRB.INFINITY and abs(var.X - var.LB) < 1e-5:
                    binding_list.append(f"{var_names[j]}_at_MIN")
                    
            return SimpleNamespace(
                success=True, status=model.Status, 
                x=np.array([var.X for var in x_vars], dtype=float), binding=binding_list
            )

        return SimpleNamespace(success=False, status=model.Status, x=np.full(len(local_bounds), np.nan))

    return SimpleNamespace(
        solve=solve_with_bounds, n_var=len(bounds), bounds=bounds, 
        theta=theta, du=du,   
        p_flex_common=p_flex_common, q_flex_common=q_flex_common, 
        pcc_p=pcc_p, pcc_q=pcc_q, pv_p=pv_p, pv_q=pv_q, hp_p_el=hp_p_el, bess_p=bess_p, bess_q=bess_q,
        add_eq=add_eq
    )
