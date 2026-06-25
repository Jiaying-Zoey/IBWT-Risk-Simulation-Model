# -*- coding: utf-8 -*-
import pandas as pd
import numpy as np
from pathlib import Path

# ================= Configuration =================
EXCEL_PATH = "Input_Data.xlsx"
USE_VMID = False  # Whether to use Vmid as the threshold

# Grid parameters
FACTOR_LIST1 = [round(x, 2) for x in np.arange(0.0, 1.001, 0.05)]
FACTOR_LIST2 = [round(x, 2) for x in np.arange(0.0, 1.001, 0.05)]

# Root output directory
OUT_ROOT = Path("Output")
OUT_ROOT.mkdir(parents=True, exist_ok=True)


# ================= Utility functions =================
def read_sheet(name: str):
    return pd.read_excel(EXCEL_PATH, sheet_name=name)


def init_output_frames(periods, users_df):
    user_cols = users_df['UserCode'].tolist()

    S_user = pd.DataFrame(0.0, index=periods, columns=user_cols)  # Water supply
    D_user = pd.DataFrame(0.0, index=periods, columns=user_cols)  # Water demand

    V = pd.DataFrame(index=periods, columns=['HZ', 'LM'], dtype=float)
    Spill = pd.DataFrame(0.0, index=periods, columns=['HZ', 'LM'])

    Qdiv = pd.DataFrame(
        0.0,
        index=periods,
        columns=['CJ_to_HZ', 'HZ_to_LM', 'LM_to_DS']
    )
    Qdiv_total = pd.DataFrame(
        0.0,
        index=periods,
        columns=['TotalDiversion']
    )

    return {
        'S_user': S_user,
        'D_user': D_user,
        'V': V,
        'Spill': Spill,
        'Qdiv': Qdiv,
        'Qdiv_total': Qdiv_total
    }


def alloc_to_group(group_name, t, S_group, dem_row, users_df, out_frames):
    """Allocate S_group to users in the specified group by priority and proportion."""
    grp_users = users_df.query("Group == @group_name").copy()
    if grp_users.empty or S_group <= 0:
        return

    remaining = float(S_group)
    for p in sorted(grp_users['Priority'].unique()):
        tier = grp_users[grp_users['Priority'] == p]
        codes = tier['UserCode'].tolist()
        needs = dem_row[codes].fillna(0.0)
        tot = float(needs.sum())
        if tot <= 0:
            continue

        give = min(remaining, tot)
        alloc = needs / tot * give
        out_frames['S_user'].loc[t, codes] += alloc.values
        remaining -= give
        if remaining <= 1e-9:
            break


# ================= Read static inputs =================
lakes = read_sheet('Lakes')
lakes['LakeCode'] = lakes['LakeCode'].astype(str)
lakes = lakes.set_index('LakeCode')

pumps = read_sheet('Pumps')
pumps['Period'] = pd.to_datetime(pumps['Period'])
pumps = pumps.set_index('Period')

plan_base = read_sheet('DiversionPlan')
plan_base['Period'] = pd.to_datetime(plan_base['Period'])
plan_base = plan_base.set_index('Period').sort_index()

users = read_sheet('Users')
users['UserCode'] = users['UserCode'].astype(str)
users['Group'] = users['Group'].astype(str)

lake_levels = read_sheet('LakeLevels')
lake_levels['Period'] = pd.to_datetime(lake_levels['Period'])
lake_levels['LakeCode'] = lake_levels['LakeCode'].astype(str)
lake_levels = lake_levels.set_index(['Period', 'LakeCode']).sort_index()

# Use the index of the diversion plan as the common set of periods
periods = plan_base.index.sort_values()

# ================= Read scenario-based dynamic inputs =================
qin_scen_raw = read_sheet('LakeInflow')
dem_scen_raw = read_sheet('Demand')

# Validate that the period columns cover all periods in DiversionPlan
period_cols_qin = [
    c for c in qin_scen_raw.columns
    if c not in ('ScenarioNum', 'LakeCode')
]
period_cols_dem = [
    c for c in dem_scen_raw.columns
    if c not in ('ScenarioNum', 'UserCode')
]

if set(periods) - set(period_cols_qin):
    raise ValueError(
        "The period columns in LakeInflow do not cover all Period values in DiversionPlan."
    )
if set(periods) - set(period_cols_dem):
    raise ValueError(
        "The period columns in Demand do not cover all Period values in DiversionPlan."
    )

qin_scen = qin_scen_raw.set_index(['ScenarioNum', 'LakeCode'])
dem_scen = dem_scen_raw.set_index(['ScenarioNum', 'UserCode'])

scenario_list = sorted(
    qin_scen.index.get_level_values(0).unique().tolist()
)

# =============== Solve one scenario ===============
def run_one_scenario(sid: int, plan_current: pd.DataFrame):
    # Inflow: Period x Lake
    qin_this = qin_scen.loc[sid, period_cols_qin]  # Rows: LakeCode; columns: Period
    qin_df = qin_this.set_index(
        pd.Index(qin_this.index, name='LakeCode')
    ).T
    qin_df = qin_df.reindex(index=periods).fillna(0.0)

    # Demand: Period x User
    dem_this = dem_scen.loc[sid, period_cols_dem]  # Rows: UserCode; columns: Period
    dem_df = dem_this.set_index(
        pd.Index(dem_this.index, name='UserCode')
    ).T
    dem_df = dem_df.reindex(index=periods).fillna(0.0)

    out = init_output_frames(periods, users)

    flow_stats = pd.DataFrame(
        0.0,
        index=periods,
        columns=[
            'Qpass_CJ_HZ', 'Qnet_CJ_HZ',
            'Qallow_HZ_LM', 'Qnet_HZ_LM',
            'Qallow_LM_DS', 'Qnet_LM_DS',
            'TotalDiversion',
            'Avail_HZ', 'Avail_LM'
        ]
    )

    # Initial storage
    V_HZ_prev = float(lakes.loc['HZ', 'V0'])
    V_LM_prev = float(lakes.loc['LM', 'V0'])

    for t in periods:
        # Record water demand
        for u in users['UserCode']:
            if u in dem_df.columns:
                out['D_user'].loc[t, u] = dem_df.at[t, u]

        # ========= Step 1: Yangtze River -> Hongze Lake =========
        Qplan_CJ = float(plan_current.at[t, 'Plan_CJ_to_HZ'])
        Qpass_CJ = min(
            Qplan_CJ,
            float(pumps.at[t, 'P_CJ_HZ_Cap'])
        )
        Qnet_CJ = Qpass_CJ * (
            1 - float(pumps.at[t, 'P_CJ_HZ_Loss'])
        )

        Qdiv_to_HZ_net = Qnet_CJ
        out['Qdiv'].loc[t, 'CJ_to_HZ'] = Qdiv_to_HZ_net

        # ========= Step 2: Hongze Lake =========
        Qplan_HZ_LM = float(plan_current.at[t, 'Plan_HZ_to_LM'])

        Avail_HZ = (
            V_HZ_prev
            + float(qin_df.at[t, 'HZ'])
            + Qdiv_to_HZ_net
        )
        row_hz = lake_levels.loc[(t, 'HZ')]
        Vmin_HZ = float(row_hz['Vmin'])
        Vmax_HZ = float(row_hz['Vmax'])
        Vmid_HZ = (
            float(row_hz['Vmid'])
            if ('Vmid' in row_hz and not pd.isna(row_hz['Vmid']))
            else Vmin_HZ
        )
        threshold_HZ = Vmid_HZ if USE_VMID else Vmin_HZ

        # HZ -> LM is constrained jointly by the threshold, pump capacity, and plan
        Qpass_HZ_LM_cap = min(
            Qplan_HZ_LM,
            float(pumps.at[t, 'P_HZ_LM_Cap'])
        )
        water_above_threshold_HZ = max(
            Avail_HZ - threshold_HZ,
            0.0
        )
        Qallow_HZ_LM = min(
            Qpass_HZ_LM_cap,
            water_above_threshold_HZ
        )

        Qnet_HZ_LM = Qallow_HZ_LM * (
            1 - float(pumps.at[t, 'P_HZ_LM_Loss'])
        )
        out['Qdiv'].loc[t, 'HZ_to_LM'] = Qnet_HZ_LM

        # Supply users served by Hongze Lake
        SupCap_HZ = (
            float(lakes.loc['HZ', 'CapSupply'])
            if 'CapSupply' in lakes.columns
            else 1e18
        )
        codes_hz = users.query(
            "Group == 'HZ_LAKE'"
        )['UserCode'].tolist()
        D_hz = (
            float(dem_df.loc[t, codes_hz].sum())
            if codes_hz
            else 0.0
        )

        supply_space_HZ = max(
            Avail_HZ - Qallow_HZ_LM - threshold_HZ,
            0.0
        )
        G_hz_to_local = min(
            D_hz,
            SupCap_HZ,
            supply_space_HZ
        )

        alloc_to_group(
            'HZ_LAKE',
            t,
            G_hz_to_local,
            dem_df.loc[t],
            users,
            out
        )

        # Finalize Hongze Lake storage and spillage
        Vtmp_HZ = Avail_HZ - Qallow_HZ_LM - G_hz_to_local
        Spill_HZ = max(Vtmp_HZ - Vmax_HZ, 0.0)
        V_HZ = Vtmp_HZ - Spill_HZ

        out['V'].loc[t, 'HZ'] = V_HZ
        out['Spill'].loc[t, 'HZ'] = Spill_HZ

        # ========= Step 3: Luoma Lake =========
        Qplan_LM_DS = (
            float(plan_current.at[t, 'Plan_LM_to_DS'])
            if 'Plan_LM_to_DS' in plan_current.columns
            else 0.0
        )
        Avail_LM = (
            V_LM_prev
            + float(qin_df.at[t, 'LM'])
            + Qnet_HZ_LM
        )

        row_lm = lake_levels.loc[(t, 'LM')]
        Vmin_LM = float(row_lm['Vmin'])
        Vmax_LM = float(row_lm['Vmax'])
        Vmid_LM = (
            float(row_lm['Vmid'])
            if ('Vmid' in row_lm and not pd.isna(row_lm['Vmid']))
            else Vmin_LM
        )
        threshold_LM = Vmid_LM if USE_VMID else Vmin_LM

        Qpass_LM_DS_cap = (
            min(
                Qplan_LM_DS,
                float(pumps.at[t, 'P_LM_OUT_Cap'])
            )
            if 'P_LM_OUT_Cap' in pumps.columns
            else Qplan_LM_DS
        )
        water_above_threshold_LM = max(
            Avail_LM - threshold_LM,
            0.0
        )
        Qallow_LM_DS = min(
            Qpass_LM_DS_cap,
            water_above_threshold_LM
        )

        Qnet_LM_DS = (
            Qallow_LM_DS
            * (1 - float(pumps.at[t, 'P_LM_OUT_Loss']))
            if 'P_LM_OUT_Loss' in pumps.columns
            else Qallow_LM_DS
        )
        out['Qdiv'].loc[t, 'LM_to_DS'] = Qnet_LM_DS

        # Supply users served by Luoma Lake
        SupCap_LM = (
            float(lakes.loc['LM', 'CapSupply'])
            if 'CapSupply' in lakes.columns
            else 1e18
        )
        codes_lm = users.query(
            "Group == 'LM_LAKE'"
        )['UserCode'].tolist()
        D_lm = (
            float(dem_df.loc[t, codes_lm].sum())
            if codes_lm
            else 0.0
        )

        supply_space_LM = max(
            Avail_LM - Qallow_LM_DS - threshold_LM,
            0.0
        )
        G_lm_to_local = min(
            D_lm,
            SupCap_LM,
            supply_space_LM
        )
        alloc_to_group(
            'LM_LAKE',
            t,
            G_lm_to_local,
            dem_df.loc[t],
            users,
            out
        )

        # Finalize Luoma Lake storage and spillage
        Vtmp_LM = Avail_LM - Qallow_LM_DS - G_lm_to_local
        Spill_LM = max(Vtmp_LM - Vmax_LM, 0.0)
        V_LM = Vtmp_LM - Spill_LM

        out['V'].loc[t, 'LM'] = V_LM
        out['Spill'].loc[t, 'LM'] = Spill_LM

        # ========= Total diversion statistics =========
        cj_net = Qnet_CJ
        hz_out_net = Qallow_HZ_LM
        lm_out_net = Qallow_LM_DS

        total_diversion = (
            cj_net
            + max(hz_out_net - cj_net, 0.0)
            + max(lm_out_net - hz_out_net, 0.0)
        )
        out['Qdiv_total'].loc[t, 'TotalDiversion'] = total_diversion

        # Record flow statistics
        flow_stats.at[t, 'Qpass_CJ_HZ'] = Qpass_CJ
        flow_stats.at[t, 'Qnet_CJ_HZ'] = Qnet_CJ
        flow_stats.at[t, 'Qallow_HZ_LM'] = Qallow_HZ_LM
        flow_stats.at[t, 'Qnet_HZ_LM'] = Qnet_HZ_LM
        flow_stats.at[t, 'Qallow_LM_DS'] = Qallow_LM_DS
        flow_stats.at[t, 'Qnet_LM_DS'] = Qnet_LM_DS
        flow_stats.at[t, 'TotalDiversion'] = total_diversion
        flow_stats.at[t, 'Avail_HZ'] = Avail_HZ
        flow_stats.at[t, 'Avail_LM'] = Avail_LM

        # Use the current ending storage as the next period's initial storage
        V_HZ_prev, V_LM_prev = V_HZ, V_LM

    out['FlowStats'] = flow_stats
    return out


# =============== Run all inflow-demand scenarios for each grid-based plan ===============
def run_all_scenarios_for_plan(
        plan_current: pd.DataFrame,
        plan_id: int,
        cj_factor: float,
        hz_factor: float):
    # Accumulators for statistics within the current plan scenario
    user_shortage_hit = pd.Series(
        0,
        index=users['UserCode'],
        dtype=int
    )
    user_shortage_sum = pd.DataFrame(
        0.0,
        index=periods,
        columns=users['UserCode']
    )

    user_shortage_hit_month = pd.DataFrame(
        0,
        index=periods,
        columns=users['UserCode']
    )
    lake_spill_hit_month = pd.DataFrame(
        0,
        index=periods,
        columns=['HZ', 'LM']
    )

    lake_spill_hit = pd.Series(
        0,
        index=['HZ', 'LM'],
        dtype=int
    )
    lake_spill_sum = pd.DataFrame(
        0.0,
        index=periods,
        columns=['HZ', 'LM']
    )

    # ESR: calculate annual shortage / annual demand for each scenario, then average
    esr_system_list = []
    esr_group_lists = {'HZ_LAKE': [], 'LM_LAKE': []}

    # Collect flows across scenarios
    qpass_all = pd.DataFrame(index=periods)
    qallow_hz_lm_all = pd.DataFrame(index=periods)
    qallow_lm_ds_all = pd.DataFrame(index=periods)
    total_div_all = pd.DataFrame(index=periods)

    # Output directories
    plan_dir = OUT_ROOT / (
        f"PlanScenario{plan_id:04d}_"
        f"CJx{cj_factor:.2f}_HZx{hz_factor:.2f}"
    )
    detail_dir = plan_dir / "detail"
    detail_dir.mkdir(parents=True, exist_ok=True)

    scenario_count = 0
    for sid in scenario_list:
        scenario_count += 1
        print(
            f"\n=== Plan scenario {plan_id:04d} | "
            f"CJx{cj_factor:.2f}, HZx{hz_factor:.2f} | "
            f"Running scenario {sid} ==="
        )

        out = run_one_scenario(sid, plan_current)

        fs = out['FlowStats']
        colname = f"Scenario{sid}"
        qpass_all[colname] = fs['Qpass_CJ_HZ'].values
        qallow_hz_lm_all[colname] = fs['Qallow_HZ_LM'].values
        qallow_lm_ds_all[colname] = fs['Qallow_LM_DS'].values
        total_div_all[colname] = fs['TotalDiversion'].values

        # Export results for the current scenario
        with pd.ExcelWriter(
            detail_dir / f"Scenario{sid}.xlsx"
        ) as w:
            out['Qdiv'].to_excel(w, sheet_name="Diversion")
            out['Qdiv_total'].to_excel(
                w,
                sheet_name="TotalDiversion"
            )
            out['V'].to_excel(w, sheet_name="LakeStorage")
            out['Spill'].to_excel(w, sheet_name="LakeSpill")
            out['S_user'].to_excel(w, sheet_name="UserSupply")
            out['D_user'].to_excel(w, sheet_name="UserDemand")
            (out['D_user'] - out['S_user']).to_excel(
                w,
                sheet_name="UserGap"
            )

        # Accumulate risk statistics
        gap_user = out['D_user'] - out['S_user']
        hit_mask = gap_user.sum(axis=0) > 1e-5
        user_shortage_hit.loc[hit_mask.index] += (
            hit_mask.astype(int).values
        )
        user_shortage_sum = user_shortage_sum.add(
            gap_user,
            fill_value=0.0
        )

        spill = out['Spill']
        lake_hit = spill.sum(axis=0) > 1e-5
        lake_spill_hit.loc[lake_hit.index] += (
            lake_hit.astype(int).values
        )
        lake_spill_sum = lake_spill_sum.add(
            spill,
            fill_value=0.0
        )

        user_shortage_hit_month += (gap_user > 1e-5).astype(int)
        lake_spill_hit_month += (spill > 1e-5).astype(int)

        # System-level ESR
        D_all = float(out['D_user'].to_numpy().sum())
        Sh_all = float(gap_user.to_numpy().sum())
        esr_system_list.append(
            (Sh_all / D_all) if D_all > 0 else 0.0
        )

        # Group-level ESR
        for gname in esr_group_lists.keys():
            codes = users.query(
                "Group == @gname"
            )['UserCode'].tolist()
            if codes:
                D_g = float(
                    out['D_user'][codes].to_numpy().sum()
                )
                Sh_g = float(
                    gap_user[codes].to_numpy().sum()
                )
                esr_group_lists[gname].append(
                    (Sh_g / D_g) if D_g > 0 else 0.0
                )
            else:
                esr_group_lists[gname].append(0.0)

    # Plan-scenario-level statistical outputs
    N = scenario_count if scenario_count > 0 else 1

    user_shortage_prob = (
        user_shortage_hit / N
    ).to_frame(name='ProbShortage')
    lake_spill_prob = (
        lake_spill_hit / N
    ).to_frame(name='ProbSpill')

    user_shortage_avg = user_shortage_sum / N  # Period x User
    lake_spill_avg = lake_spill_sum / N  # Period x Lake

    user_shortage_prob_month = user_shortage_hit_month / N
    lake_spill_prob_month = lake_spill_hit_month / N

    # Add the Mean column
    if qpass_all.shape[1] > 0:
        qpass_all['Mean'] = qpass_all.mean(axis=1)
    if qallow_hz_lm_all.shape[1] > 0:
        qallow_hz_lm_all['Mean'] = qallow_hz_lm_all.mean(axis=1)
    if qallow_lm_ds_all.shape[1] > 0:
        qallow_lm_ds_all['Mean'] = qallow_lm_ds_all.mean(axis=1)
    if total_div_all.shape[1] > 0:
        total_div_all['Mean'] = total_div_all.mean(axis=1)

    # Export statistical files
    with pd.ExcelWriter(
        plan_dir / "Risk_Statistics.xlsx"
    ) as w:
        user_shortage_prob.to_excel(
            w,
            sheet_name="UserShortageProb"
        )
        user_shortage_avg.to_excel(
            w,
            sheet_name="UserShortageAvg"
        )
        lake_spill_prob.to_excel(
            w,
            sheet_name="LakeSpillProb"
        )
        lake_spill_avg.to_excel(
            w,
            sheet_name="LakeSpillAvg"
        )
        user_shortage_prob_month.to_excel(
            w,
            sheet_name="UserShortageProbMonthly"
        )
        lake_spill_prob_month.to_excel(
            w,
            sheet_name="LakeSpillProbMonthly"
        )

    with pd.ExcelWriter(
        plan_dir / "Diversion_Statistics.xlsx"
    ) as w:
        qpass_all.to_excel(w, sheet_name="Qpass_CJ_to_HZ")
        qallow_hz_lm_all.to_excel(
            w,
            sheet_name="Qallow_HZ_to_LM"
        )
        qallow_lm_ds_all.to_excel(
            w,
            sheet_name="Qallow_LM_to_DS"
        )
        total_div_all.to_excel(w, sheet_name="TotalDiversion")

    # ESR
    ESR_system = (
        float(np.mean(esr_system_list))
        if esr_system_list
        else 0.0
    )
    ESR_groups = {
        g: float(np.mean(v)) if v else 0.0
        for g, v in esr_group_lists.items()
    }

    # Annual indicators
    total_diversion_annual = (
        float(total_div_all['Mean'].sum())
        if 'Mean' in total_div_all.columns
        else 0.0
    )
    cj_to_hz_annual = (
        float(qpass_all['Mean'].sum())
        if 'Mean' in qpass_all.columns
        else 0.0
    )
    hz_to_lm_annual = (
        float(qallow_hz_lm_all['Mean'].sum())
        if 'Mean' in qallow_hz_lm_all.columns
        else 0.0
    )
    lm_to_ds_annual = (
        float(qallow_lm_ds_all['Mean'].sum())
        if 'Mean' in qallow_lm_ds_all.columns
        else 0.0
    )

    hz_spill_annual = float(lake_spill_avg['HZ'].sum())
    lm_spill_annual = float(lake_spill_avg['LM'].sum())
    total_exp_spill_annual = (
        hz_spill_annual + lm_spill_annual
    )

    def sum_shortage_by_group(group_name):
        codes = users.query(
            "Group == @group_name"
        )['UserCode'].tolist()
        if not codes:
            return 0.0
        return float(user_shortage_avg[codes].sum().sum())

    hz_short_annual = sum_shortage_by_group('HZ_LAKE')
    lm_short_annual = sum_shortage_by_group('LM_LAKE')
    total_exp_short_annual = float(
        user_shortage_avg.sum().sum()
    )

    print(
        f"\nPlan scenario {plan_id:04d} | "
        f"CJx{cj_factor:.2f}, HZx{hz_factor:.2f} completed. "
        f"Output directory: {plan_dir}"
    )

    return {
        'PlanScenarioID': plan_id,
        'CJFactor': cj_factor,
        'HZFactor': hz_factor,

        'AnnualActualNorthboundDiversion_Net': total_diversion_annual,
        'AnnualYangtzeDiversion': cj_to_hz_annual,
        'AnnualHZtoLMDiversion': hz_to_lm_annual,
        'AnnualLMtoDSDiversion': lm_to_ds_annual,

        'TotalExpectedSpillage': total_exp_spill_annual,
        'AnnualExpectedSpillage_HZ': hz_spill_annual,
        'AnnualExpectedSpillage_LM': lm_spill_annual,

        'TotalExpectedShortage': total_exp_short_annual,
        'AnnualExpectedShortage_HZUsers': hz_short_annual,
        'AnnualExpectedShortage_LMUsers': lm_short_annual,

        'SystemESR': ESR_system,
        'HZUsersESR': ESR_groups.get('HZ_LAKE', 0.0),
        'LMUsersESR': ESR_groups.get('LM_LAKE', 0.0),
    }


# =============== Outer dual-grid loops ===============
summary_rows = []
plan_id = 0

for cj_f in FACTOR_LIST1:
    for hz_f in FACTOR_LIST2:
        plan_id += 1

        plan_scaled = plan_base.copy()

        # Outer loop: scale Plan_CJ_to_HZ
        plan_scaled['Plan_CJ_to_HZ'] = (
            plan_scaled['Plan_CJ_to_HZ'].astype(float) * cj_f
        )

        # Inner loop: scale Plan_HZ_to_LM
        plan_scaled['Plan_HZ_to_LM'] = (
            plan_scaled['Plan_HZ_to_LM'].astype(float) * hz_f
        )

        if 'Plan_LM_to_DS' in plan_scaled.columns:
            plan_scaled['Plan_LM_to_DS'] = (
                plan_scaled['Plan_LM_to_DS'].astype(float)
            )

        res = run_all_scenarios_for_plan(
            plan_scaled,
            plan_id=plan_id,
            cj_factor=cj_f,
            hz_factor=hz_f
        )
        summary_rows.append(res)

# =============== Export the overall summary table ===============
summary_df = pd.DataFrame(summary_rows).sort_values(
    'PlanScenarioID'
).reset_index(drop=True)
out_summary = OUT_ROOT / "Summary_Dual_Grid_Two_Lakes.xlsx"
summary_df.to_excel(out_summary, index=False)
