"""Validate / analyze adc_dac_ideal_4b: 4-bit ideal ADC → DAC round-trip.

validate_csv(out_dir)  — called by the pytest suite; returns failure count.
Run as a script        — runs both testbenches and saves two figures.

Figure 1 (ramp input):
  top    clock
  middle ramp input
  bottom ADC code + DAC vout  (overlay, twin y-axes)

Figure 2 (sine input, first 3 post-reset cycles):
  top    vin + vout overlay
  middle dout_code
  bottom quantisation error (mV)
"""
import importlib
import time
from pathlib import Path

import matplotlib
import numpy as np
from matplotlib.ticker import MultipleLocator

matplotlib.use('Agg')
import matplotlib.pyplot as plt


def evas_simulate(*args, **kwargs):
    module = importlib.import_module("evas.netlist.runner")
    return module.evas_simulate(*args, **kwargs)

HERE = Path(__file__).parent
OUT  = HERE.parent.parent.parent / 'output' / 'adc_dac_ideal_4b'

# Ramp testbench: rst_n deasserts at 10 ns; 2-cycle guard → 12 ns
RST_END_RAMP_NS = 12.0
RST_END_RAMP_S  = RST_END_RAMP_NS * 1e-9

# Sine testbench: rst_n deasserts at 2 ns; 1-cycle guard → 3 ns
RST_END_SINE_NS = 3.0
RST_END_SINE_S  = RST_END_SINE_NS * 1e-9

VDD   = 0.9
N_LVL = 16
VSTEP = VDD / N_LVL   # 1 LSB in volts


# ── Validation function (called by pytest) ────────────────────────────────────

def validate_csv(out_dir=None):
    """Check the sine simulation CSV for correctness. Returns failure count."""
    if out_dir is None:
        out_dir = OUT / 'sine'
    out_dir = Path(out_dir)

    data  = np.genfromtxt(out_dir / 'tran.csv', delimiter=',', names=True, dtype=None, encoding='utf-8')
    post  = data['time'] > RST_END_SINE_S
    code  = data['dout_code'].astype(float)[post]
    vout  = data['vout'][post]
    vin   = data['vin'][post]

    failures = 0

    unique_codes = set(int(round(c)) for c in code)
    n_unique = len(unique_codes)
    if n_unique < N_LVL // 2:
        print(f"FAIL: only {n_unique} distinct codes appear post-reset (expected ≥ {N_LVL // 2})")
        failures += 1
    if min(unique_codes) > 1 or max(unique_codes) < N_LVL - 2:
        print(f"FAIL: code range [{min(unique_codes)}..{max(unique_codes)}] does not span [0..{N_LVL-1}]")
        failures += 1

    if vout.min() < -0.05 or vout.max() > VDD + 0.05:
        print(f"FAIL: vout range [{vout.min():.3f}, {vout.max():.3f}]")
        failures += 1

    if np.abs(code - np.round(code)).max() > 0.01:
        print("FAIL: dout_code has non-integer values")
        failures += 1

    # 截断型 ADC：采样时刻误差应在 (-LSB, 0]，|误差| < 1 LSB
    # 用 code×VSTEP 代替 vout，避免 DAC transition 延迟引入的误差
    code_int = np.round(code).astype(int)
    sample_mask = np.concatenate(([True], np.diff(code_int) != 0))
    vin_s  = vin[sample_mask]
    code_s = code_int[sample_mask]
    q_err  = code_s * VSTEP - vin_s   # 截断型：应在 (-LSB, 0]
    if q_err.max() > 1e-6:
        print(f"FAIL: q_error > 0 at sample instants (max = {q_err.max()*1e3:.2f} mV), expected truncation ≤ 0")
        failures += 1
    if np.abs(q_err).max() >= VSTEP:
        print(f"FAIL: |q_error| = {np.abs(q_err).max()*1e3:.1f} mV ≥ 1 LSB ({VSTEP*1e3:.1f} mV)")
        failures += 1

    return failures


# ── Standalone: simulate + plot ───────────────────────────────────────────────

if __name__ == '__main__':
    OUT_RAMP      = OUT / 'ramp'
    OUT_SINE      = OUT / 'sine'
    OUT_SINE1000  = OUT / 'sine1000'

    t0 = time.perf_counter()
    evas_simulate(str(HERE / 'tb_adc_dac_ideal_4b_ramp.scs'), output_dir=str(OUT_RAMP))
    t_ramp = time.perf_counter() - t0

    t0 = time.perf_counter()
    evas_simulate(str(HERE / 'tb_adc_dac_ideal_4b_sine.scs'), output_dir=str(OUT_SINE))
    t_sine = time.perf_counter() - t0

    t0 = time.perf_counter()
    evas_simulate(str(HERE / 'tb_adc_dac_ideal_4b_sine1000.scs'), output_dir=str(OUT_SINE1000))
    t_sine1000 = time.perf_counter() - t0

    failures = validate_csv(OUT_SINE)
    print(f"Validation: {failures} failure(s)")

    # ── Figure 1: Ramp ────────────────────────────────────────────────────────
    df_r  = np.genfromtxt(OUT_RAMP / 'tran.csv', delimiter=',', names=True, dtype=None, encoding='utf-8')
    t_ns  = df_r['time'] * 1e9
    post  = t_ns > RST_END_RAMP_NS
    t_r   = t_ns[post]
    clk_r = df_r['clk'][post]
    vin_r = df_r['vin'][post]
    vout_r = df_r['vout'][post]
    code_r = df_r['dout_code'].astype(float)[post]

    fig1, axes = plt.subplots(3, 1, figsize=(12, 7), sharex=True,
                              gridspec_kw={'height_ratios': [1, 1.5, 2]})
    fig1.suptitle(
        f'4-bit Ideal ADC -> DAC  --  Ramp input  [{t_ramp:.3f} s]',
        fontsize=11)

    ax = axes[0]
    ax.plot(t_r, clk_r, linewidth=1.0, color='gray')
    ax.set_ylabel('clk (V)')
    ax.set_ylim(-VDD * 0.1, VDD * 1.2)
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.plot(t_r, vin_r, linewidth=1.0, color='C0', label='vin (ramp)')
    ax.set_ylabel('vin (V)')
    ax.legend(loc='upper left', fontsize=9)
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    ax2 = ax.twinx()
    l1, = ax.step(t_r, code_r, where='post', linewidth=1.0, color='C0', label='ADC code')
    l2, = ax2.plot(t_r, vout_r, linewidth=1.0, color='C1', linestyle='--', label='vout (DAC)')
    ax.set_ylabel('ADC code (0-15)')
    ax.set_ylim(-0.5, 15.5)
    ax.yaxis.set_major_locator(MultipleLocator(4))
    ax2.set_ylabel('vout (V)')
    ax2.set_ylim(-0.5 * VSTEP, 15.5 * VSTEP)
    ax.set_xlabel('Time (ns)')
    ax.legend(handles=[l1, l2], loc='upper left', fontsize=9)
    ax.grid(True, alpha=0.3)

    fig1.tight_layout()
    p1 = OUT / 'fig1_ramp.png'
    fig1.savefig(str(p1), dpi=150, bbox_inches='tight')
    plt.close(fig1)
    print(f"Saved: {p1}")

    # ── Figure 2: Sine (first 3 post-reset cycles) ────────────────────────────
    df_s   = np.genfromtxt(OUT_SINE / 'tran.csv', delimiter=',', names=True, dtype=None, encoding='utf-8')
    t_ns   = df_s['time'] * 1e9
    win3   = t_ns > RST_END_SINE_NS
    t_w    = t_ns[win3]
    vin_w  = df_s['vin'][win3]
    vinsh_w = df_s['vin_sh'][win3]
    vout_w = df_s['vout'][win3]
    code_w = df_s['dout_code'].astype(float)[win3]

    fig2, axes = plt.subplots(3, 1, figsize=(12, 7), sharex=True,
                              gridspec_kw={'height_ratios': [2, 1.5, 2]})
    fig2.suptitle(
        f'4-bit Ideal ADC -> DAC  --  Sine input (63 samples/cycle, 1 cycle post-reset)  [{t_sine:.3f} s]',
        fontsize=11)

    ax = axes[0]
    ax.plot(t_w, vin_w,   linewidth=1.0, label='vin (sine)')
    ax.plot(t_w, vinsh_w, linewidth=1.0, linestyle=':', color='C2', label='vin_sh (S&H)')
    ax.plot(t_w, vout_w,  linewidth=1.0, linestyle='--', label='vout (DAC)')
    ax.set_ylabel('Voltage (V)')
    ax.legend(loc='upper right', fontsize=9)
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.step(t_w, code_w, where='post', linewidth=1.0, color='C2')
    ax.set_ylabel('dout_code')
    ax.set_ylim(-0.5, 15.5)
    ax.yaxis.set_major_locator(MultipleLocator(4))
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    q_err_mv = (vout_w - vinsh_w) * 1e3
    ax.plot(t_w, q_err_mv, linewidth=1.0, color='C3')
    ax.axhline(0, color='k', linewidth=1.0, linestyle='--')
    lsb_mv = VSTEP * 1e3
    ax.axhline(-lsb_mv, color='gray', linewidth=1.0, linestyle=':', label='+-1 LSB')
    ax.axhline( lsb_mv, color='gray', linewidth=1.0, linestyle=':')
    ax.set_ylabel('quant. error vout-vin_sh (mV)')
    ax.set_xlabel('Time (ns)')
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    fig2.tight_layout()
    p2 = OUT / 'fig2_sine.png'
    fig2.savefig(str(p2), dpi=150, bbox_inches='tight')
    plt.close(fig2)
    print(f"Saved: {p2}")

    # -- Figure 3: Sine 1000 samples/cycle (1 post-reset cycle) ---------------
    df_s3    = np.genfromtxt(OUT_SINE1000 / 'tran.csv', delimiter=',', names=True, dtype=None, encoding='utf-8')
    t_ns     = df_s3['time'] * 1e9
    win1     = t_ns > RST_END_SINE_NS
    t_w3     = t_ns[win1]
    vin_w3   = df_s3['vin'][win1]
    vinsh_w3 = df_s3['vin_sh'][win1]
    vout_w3  = df_s3['vout'][win1]
    code_w3  = df_s3['dout_code'].astype(float)[win1]

    fig3, axes = plt.subplots(3, 1, figsize=(12, 7), sharex=True,
                              gridspec_kw={'height_ratios': [2, 1.5, 2]})
    fig3.suptitle(
        f'4-bit Ideal ADC -> DAC  --  Sine input (1000 samples/cycle, 1 cycle post-reset)  [{t_sine1000:.3f} s]',
        fontsize=11)

    ax = axes[0]
    ax.plot(t_w3, vin_w3,   linewidth=1.0, label='vin (sine)')
    ax.plot(t_w3, vinsh_w3, linewidth=1.0, linestyle=':', color='C2', label='vin_sh (S&H)')
    ax.plot(t_w3, vout_w3,  linewidth=1.0, linestyle='--', label='vout (DAC)')
    ax.set_ylabel('Voltage (V)')
    ax.legend(loc='upper right', fontsize=9)
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.step(t_w3, code_w3, where='post', linewidth=1.0, color='C2')
    ax.set_ylabel('dout_code')
    ax.set_ylim(-0.5, 15.5)
    ax.yaxis.set_major_locator(MultipleLocator(4))
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    q_err_mv3 = (vout_w3 - vinsh_w3) * 1e3
    ax.plot(t_w3, q_err_mv3, linewidth=1.0, color='C3')
    ax.axhline(0, color='k', linewidth=1.0, linestyle='--')
    lsb_mv = VSTEP * 1e3
    ax.axhline(-lsb_mv, color='gray', linewidth=1.0, linestyle=':', label='+-1 LSB')
    ax.axhline( lsb_mv, color='gray', linewidth=1.0, linestyle=':')
    ax.set_ylabel('quant. error vout-vin_sh (mV)')
    ax.set_xlabel('Time (ns)')
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    fig3.tight_layout()
    p3 = OUT / 'fig3_sine1000.png'
    fig3.savefig(str(p3), dpi=150, bbox_inches='tight')
    plt.close(fig3)
    print(f"Saved: {p3}")
