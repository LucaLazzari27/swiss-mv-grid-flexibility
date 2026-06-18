"""
Defines the simulation scenario, technical assumptions, optional grid-scale
assets, reinforcement choices, and paths used by the FFOR modules. 
Code uses internal grid-data bus IDs. The report labels each node one number lower;
for example, code bus 130 corresponds to report node N129.
"""
import os
import math
from pathlib import Path

# ==========================================
# GRID & SIMULATION CONFIGURATION
# ==========================================
GRID_NAME = os.getenv("FFOR_GRID_NAME", "20_0")
DATA_YEAR = int(os.getenv("FFOR_DATA_YEAR", "2030"))
TARGET_TIME = os.getenv("FFOR_TARGET_TIME", "12-15 12:00:00") # MM-DD HH:MM:SS format
START_TIMESTAMP = TARGET_TIME

# ==========================================
# TEMPORAL PARAMETERS
# ==========================================
N_TIMESTEPS = int(os.getenv("FFOR_N_TIMESTEPS", os.getenv("FFOR_D", "1")))
DT_H = float(os.getenv("FFOR_DT_H", "1.0"))  # Hours per timestep (1.0 = hourly profiles)

# ==========================================
# TECHNICAL & SYSTEM PARAMETERS
# ==========================================
SBASE_MVA = 100.0
SBASE_KW = SBASE_MVA * 1000.0
SBASE_KVAR = SBASE_MVA * 1000.0

PF_DER = 0.95
TAN_PHI = math.tan(math.acos(PF_DER))

# BESS parameters
# Distributed BESS
SOC_INITIAL = float(os.getenv("FFOR_SOC_INITIAL", "0.60"))
SOC_MIN = float(os.getenv("FFOR_SOC_MIN", "0.40"))
SOC_MAX = float(os.getenv("FFOR_SOC_MAX", "0.80"))

# Grid-Scale BESS
GRID_SOC_INITIAL = float(os.getenv("FFOR_GRID_SOC_INITIAL", "0.50"))
GRID_SOC_MIN = float(os.getenv("FFOR_GRID_SOC_MIN", "0.20"))
GRID_SOC_MAX = float(os.getenv("FFOR_GRID_SOC_MAX", "0.80"))

ENFORCE_FINAL_SOC = os.getenv("FFOR_ENFORCE_FINAL_SOC", "0").lower() in {"1", "true", "yes"}
SOC_FINAL_TOL = float(os.getenv("FFOR_SOC_FINAL_TOL", "0.02"))

# Heat-pump parameters
T_SET_C = float(os.getenv("FFOR_T_SET_C", "20.0"))
T_BAND_C = float(os.getenv("FFOR_T_BAND_C", "1.0"))
T_MIN_C = T_SET_C - T_BAND_C
T_MAX_C = T_SET_C + T_BAND_C
ENFORCE_FINAL_TEMPERATURE = os.getenv("FFOR_ENFORCE_FINAL_TEMPERATURE", "0").lower() in {"1", "true", "yes"}
T_FINAL_TOL = float(os.getenv("FFOR_T_FINAL_TOL", "0.25"))

HP_POWER_MARGIN_FACTOR = float(os.getenv("FFOR_HP_POWER_MARGIN_FACTOR", "0.9"))
if HP_POWER_MARGIN_FACTOR <= 0.0:
    raise ValueError("HP_POWER_MARGIN_FACTOR must be positive")

# QuickFlex and solver parameters
N_POLY = 16
INNER_POLY_RADIUS = math.cos(math.pi / N_POLY)
FFOR_EPSILON = 1e-10

TOL_LINE_BINDING = 1e-3
TOL_VOLT_BINDING = 1e-3
TOL_LINE_NEAR = 0.03
TOL_VOLT_NEAR = 0.01
TOL_DER = 1e-5

# ==========================================
# PATHS
# ==========================================
DELIVERY_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("FFOR_DATA_ROOT", str(DELIVERY_DIR))).resolve()
OUTPUT_DIR = DELIVERY_DIR

PROFILE_DIR = DATA_ROOT / "outputs" / f"{GRID_NAME}_profiles_mveq_evbase_{DATA_YEAR}"
FFOR_INPUT_DIR = PROFILE_DIR / "ffor_inputs"

# ==========================================
# GRID-SCALE ASSETS
# ==========================================
# Optional grid-scale BESS installations.
GRID_SCALE_BESS = [
    # {
    #     "bus": 130,  # Report label: N129
    #     "p_nom_kW": 3000.0,
    #     "capacity_kWh": 7500,
    #     "is_grid_scale": True,
    #     "soc_min": GRID_SOC_MIN,
    #     "soc_max": GRID_SOC_MAX,
    #     "soc_initial": GRID_SOC_INITIAL
    # }
]

# Optional grid-scale PV installations.
GRID_SCALE_PV = [
    # {
    #     "bus": 43,
    #     "p_peak_kW": 3000.0
    # },
    # {
    #     "bus": 11,
    #     "p_peak_kW": 5000.0
    # },
    # {
    #     "bus": 104,
    #     "p_peak_kW": 4000.0
    # }
]
# ==========================================
# GRID REINFORCEMENT
# ==========================================

# Lines are identified using internal/MATPOWER bus IDs.
# Leave empty for the original grid.
REINFORCED_PHYSICAL_LINES = [
    # (9, 45),
    # (43, 44),
    # (43, 104),
    # (9, 10),
    # (44, 45),
    # (11, 25),
    # (10, 11),
]

OVERHEAD_REINFORCEMENT = {
    "r_ohm_per_km": 0.4132,
    "x_ohm_per_km": 0.339,
    "s_nom_mva": 5.8,
}

CABLE_REINFORCEMENT = {
    "r_ohm_per_km": 0.206,
    "x_ohm_per_km": 0.11,
    "s_nom_mva": 6.3,
    "c_nf_per_km": 360.0,
}

REINFORCEMENT_F_HZ = 50.0
