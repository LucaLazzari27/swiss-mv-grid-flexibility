"""
computes the baseline operating point, explores the FFOR boundary with
QuickFlex, and saves the retained boundary vertices and final plot.
"""
import numpy as np
import pandas as pd
import math
import matplotlib.pyplot as plt
from types import SimpleNamespace
from pathlib import Path
import config
import model_data
import optimization

def polygon_area(points):
    """Calculates the area of a polygon given its vertices using the Shoelace formula."""
    if len(points) < 3:
        return 0.0
    x = [p[0] for p in points]
    y = [p[1] for p in points]
    return 0.5 * abs(sum(x[i] * y[i - 1] - x[i - 1] * y[i] for i in range(len(points))))

def point_key(point, digits=10):
    return tuple(np.round(np.asarray(point, dtype=float), digits))

def convex_hull(points):
    unique = sorted({point_key(p): tuple(np.asarray(p, dtype=float)) for p in points}.values())
    if len(unique) <= 1: return [np.asarray(p, dtype=float) for p in unique]
    def cross(o, a, b): return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower = []
    for p in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 1e-12: lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 1e-12: upper.pop()
        upper.append(p)
    return [np.asarray(p, dtype=float) for p in lower[:-1] + upper[:-1]]

def outward_normal_for_segment(A, B, polygon_points):
    A, B = np.asarray(A, dtype=float), np.asarray(B, dtype=float)
    d = B - A
    norm = np.linalg.norm(d)
    n = np.array([-d[1], d[0]], dtype=float) / norm
    centroid = np.mean(np.asarray(polygon_points, dtype=float), axis=0)
    if np.dot(n, centroid - 0.5 * (A + B)) > 0.0: n = -n
    return n

def main():
    output_dir = Path(__file__).resolve().parent

    # Load data and construct the reusable linear model.
    data = model_data.load_data()
    opt = optimization.build_optimization_model(data)

    N_TIMEPOINTS = config.N_TIMESTEPS + 1
    N_CONTROL = N_TIMEPOINTS

    # Fix flexible assets to their baseline dispatch.
    baseline_bounds = list(opt.bounds)
    baseline_bounds[opt.p_flex_common] = (0.0, 0.0)
    baseline_bounds[opt.q_flex_common] = (0.0, 0.0)
    
    # Baseline: all PV, including added GRID_SCALE_PV, injects available active power
    # at unity power factor. Flexibility runs can still curtail/use reactive power.
    for a, asset in enumerate(data.pv_assets):
        for t in range(N_CONTROL):
            p_base = asset["p_base_kW_t"][t]
            baseline_bounds[opt.pv_p[(a, t)]] = (p_base, p_base)
            baseline_bounds[opt.pv_q[(a, t)]] = (0.0, 0.0)
                
    # Baseline heat-pump consumption follows the input profile.
    for a, asset in enumerate(data.hp_assets):
        for t in range(N_TIMEPOINTS):
            baseline_bounds[opt.hp_p_el[(a, t)]] = (asset["p_base_kW_t"][t], asset["p_base_kW_t"][t])
            
    # Baseline: no BESS active or reactive dispatch. BESS remains flexible later.
    for a, asset in enumerate(data.bess_assets):
        for t in range(N_CONTROL):
            baseline_bounds[opt.bess_p[(a, t)]] = (0.0, 0.0)
            baseline_bounds[opt.bess_q[(a, t)]] = (0.0, 0.0)

    # Solve the baseline operating point.
    c_base = np.zeros(opt.n_var)
    base_result = opt.solve(c_base, baseline_bounds)
    
    if not base_result.success:
        raise RuntimeError("Baseline optimization failed.")

    P_BASE_IMPORT_PU_t = np.array([float(base_result.x[opt.pcc_p[t]]) for t in range(N_TIMEPOINTS)])
    Q_BASE_IMPORT_PU_t = np.array([float(base_result.x[opt.pcc_q[t]]) for t in range(N_TIMEPOINTS)])

    # Enforce a common sustained PCC deviation from the baseline.
    for t in range(N_TIMEPOINTS):
        opt.add_eq({opt.pcc_p[t]: 1.0, opt.p_flex_common: 1.0}, P_BASE_IMPORT_PU_t[t])
        opt.add_eq({opt.pcc_q[t]: 1.0, opt.q_flex_common: 1.0}, Q_BASE_IMPORT_PU_t[t])

    # Explore the FFOR boundary using the QuickFlex procedure.
    points = []; point_solutions = {}
    point_binding_reasons = {}
    
    # Solve one directional FFOR problem:
    # minimize alpha * P_flex_common + beta * Q_flex_common.
    # QuickFlex varies (alpha, beta) to identify supporting boundary points.
    def solve_dir(alpha, beta, local_bounds=None):
        c = np.zeros(opt.n_var)
        c[opt.p_flex_common], c[opt.q_flex_common] = alpha, beta
        res = opt.solve(c, opt.bounds if local_bounds is None else local_bounds)
        if not res.success: return SimpleNamespace(success=False)
        
        p_flex, q_flex = float(res.x[opt.p_flex_common]), float(res.x[opt.q_flex_common])
        
        raw_binds = [b for b in res.binding if "pcc_" not in b and "flex_common" not in b]

        filtered_binds = []
        for b in raw_binds:
            if "Limite_Q+" in b and b.replace("Limite_Q+", "Limite_Q-") in raw_binds: continue
            if "Limite_Q-" in b and b.replace("Limite_Q-", "Limite_Q+") in raw_binds: continue
            filtered_binds.append(b)

        summary = {}
        for b in filtered_binds:
            if "Limite_Q" in b:
                key = "PV_Reactive_Limits"
            elif b.startswith("Lim_S_BESS"):
                key = "BESS_Inverter_Limit"
            elif b.startswith("Line_"):
                key = "_".join(b.split("_")[:4]) 
            elif "_at_" in b:
                limit_type = b.split("_at_")[-1]
                if b.startswith("pv_p_"): key = f"PV_Active_{limit_type}"
                elif b.startswith("pv_q_"): key = f"PV_Reactive_{limit_type}"
                elif b.startswith("bess_p_"): key = f"BESS_Active_{limit_type}"
                elif b.startswith("bess_q_"): key = f"BESS_Reactive_{limit_type}"
                elif b.startswith("bess_soc_"): key = f"BESS_SOC_{limit_type}"
                elif b.startswith("hp_p_el_"): key = f"HeatPump_Active_{limit_type}"
                elif b.startswith("hp_temp_"): key = f"HeatPump_Temp_{limit_type}"
                elif b.startswith("theta_"): key = f"Voltage_Angle_{limit_type}"
                elif b.startswith("du_"): key = f"Voltage_Mag_{limit_type}"
                else: key = b
            else:
                key = b
                
            summary[key] = summary.get(key, 0) + 1
            
        # Aggregate repeated binding constraints into compact labels.
        meaningful_binds = [f"{k} (x{v})" if v > 1 else k for k, v in summary.items()]
        bind_str = " | ".join(meaningful_binds) if meaningful_binds else "None"
        return SimpleNamespace(success=True, p_flex=p_flex, q_flex=q_flex, binding=bind_str)

    def record_point(sol):
        if not sol.success:
            return
        pt = np.array([sol.p_flex, sol.q_flex], dtype=float)
        k = point_key(pt)
        if k not in point_solutions:
            points.append(pt)
        point_solutions[k] = pt
        point_binding_reasons[k] = sol.binding

    def solve_tie_breakers(fixed_var, fixed_value, tie_dirs):
        tie_tol = 1e-9
        local_bounds = list(opt.bounds)
        local_bounds[fixed_var] = (fixed_value - tie_tol, fixed_value + tie_tol)
        for alpha, beta in tie_dirs:
            record_point(solve_dir(alpha, beta, local_bounds))

    initial_directions = [
        ("min_P", 1, 0, opt.p_flex_common, "P"),
        ("max_P", -1, 0, opt.p_flex_common, "P"),
        ("min_Q", 0, 1, opt.q_flex_common, "Q"),
        ("max_Q", 0, -1, opt.q_flex_common, "Q"),
    ]

    for name, alpha, beta, fixed_var, axis in initial_directions:
        sol = solve_dir(alpha, beta)
        if sol.success:
            record_point(sol)
            if axis == "P":
                solve_tie_breakers(fixed_var, sol.p_flex, [(0, 1), (0, -1)])
            else:
                solve_tie_breakers(fixed_var, sol.q_flex, [(1, 0), (-1, 0)])

    iteration = 1
    inactive_segments = set()
    
    while iteration <= 200:
        hull = convex_hull(points)
        current_area = polygon_area(hull) # Previous QuickFlex polygon area, A_(k-1)
        added_this_iteration = False

        for edge_idx in range(len(hull)):
            A_pt, B_pt = hull[edge_idx], hull[(edge_idx + 1) % len(hull)]
            segment_key = (point_key(A_pt), point_key(B_pt))
            
            if segment_key in inactive_segments: 
                continue

            # Maximize flexibility along the edge's outward normal.
            n = outward_normal_for_segment(A_pt, B_pt, hull)
            sol = solve_dir(float(-n[0]), float(-n[1]))
            
            if not sol.success: 
                inactive_segments.add(segment_key)
                continue

            new_point = np.array([sol.p_flex, sol.q_flex], dtype=float)
            k = point_key(new_point)
            
            # A previously explored optimum closes this segment.
            if k in point_solutions:
                inactive_segments.add(segment_key)
                continue

            # Evaluate the absolute polygon-area increase.
            hypothetical_points = points + [new_point]
            new_hull = convex_hull(hypothetical_points)
            new_area = polygon_area(new_hull) # Candidate QuickFlex polygon area, A_k
            
            area_increase = max(0.0, new_area - current_area)

            # Stop refining segments whose area contribution is below the tolerance.
            if area_increase < config.FFOR_EPSILON:
                inactive_segments.add(segment_key)
                continue

            # Accept the point and rebuild the hull before exploring another edge.
            point_solutions[k] = new_point
            point_binding_reasons[k] = sol.binding
            points.append(new_point)
            added_this_iteration = True
            break

        if not added_this_iteration: 
            break
        iteration += 1

    # Save the final boundary and plot.
    final_hull = convex_hull(points)
    
    # Associate each retained hull vertex with its active constraints.
    binding_col = [point_binding_reasons.get(point_key(pt), "Unknown") for pt in final_hull]
    
    out_df = pd.DataFrame(final_hull, columns=["P_flex_pu", "Q_flex_pu"])
    out_df["P_flex_MW"] = out_df["P_flex_pu"] * config.SBASE_MVA
    out_df["Q_flex_MVAr"] = out_df["Q_flex_pu"] * config.SBASE_MVA
    out_df["Limiting_Constraints"] = binding_col
    
    out_path_csv = output_dir / "ffor_boundary.csv"
    out_df.to_csv(out_path_csv, index=False)
    
    plt.figure(figsize=(8, 6))
    
    # Close the polygon loop for plotting.
    plot_points = np.vstack([final_hull, final_hull[0]])
    
    P_pu = plot_points[:, 0]
    Q_pu = plot_points[:, 1]

    plt.plot(P_pu, Q_pu, color='blue', linewidth=2, label="FFOR Boundary")
    plt.fill(P_pu, Q_pu, color='blue', alpha=0.15)
    
    # Highlight the operating points retained by QuickFlex.
    plt.scatter(P_pu[:-1], Q_pu[:-1], color='red', zorder=5, s=25, label="Operating Points")

    plt.axhline(0, color='black', linewidth=1)
    plt.axvline(0, color='black', linewidth=1)
    plt.xlabel(r"Active Power Flexibility $\Delta P$ (p.u.)", fontsize=12)
    plt.ylabel(r"Reactive Power Flexibility $\Delta Q$ (p.u.)", fontsize=12)
    plt.title(f"FFOR Boundary - Grid: {config.GRID_NAME} | Time: {config.TARGET_TIME}", fontsize=14)
    plt.grid(True, linestyle="--", alpha=0.6)
    plt.legend()
    
    out_path_img = output_dir / "ffor_boundary.png"
    plt.savefig(out_path_img, dpi=300, bbox_inches='tight')
    plt.close()

    print(f"FFOR saved to {out_path_csv} and {out_path_img}")

if __name__ == "__main__":
    main()
