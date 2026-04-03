"""Analyze adc_dac_ideal_4b: 4-bit ideal ADC -> DAC round-trip (ramp + sine)."""
import time
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use('Agg')
import matplotlib.pyplot as plt

from evas.netlist.runner import evas_simulate

HERE = Path(__file__).parent
_DEFAULT_OUT = HERE.parent.parent.parent / 'output' / 'adc_dac_ideal_4b'

VDD   = 0.9
N_LVL = 16
VSTEP = VDD / N_LVL   # 1 LSB in volts

RST_END_RAMP_NS = 12.0
RST_END_SINE_NS = 3.0


def _plot_ramp(out_dir: Path, wall_s: float) -> None:
    data = np.genfromtxt(out_dir / 'tran.csv', delimiter=',', names=True, dtype=None, encoding='utf-8')
    t_ns = data['time'] * 1e9
    post = t_ns > RST_END_RAMP_NS
    t    = t_ns[post]
    clk  = data['clk'][post]
    vin  = data['vin'][post]
    vout = data['vout'][post]
    code = data['dout_code'].astype(float)[post]

    fig, axes = plt.subplots(3, 1, figsize=(12, 7), sharex=True,
                             gridspec_kw={'height_ratios': [1, 1.5, 2]})

    ax = axes[0]
    ax.plot(t, clk, linewidth=1.0, color='gray', drawstyle='steps-post')
    ax.set_ylabel('clk (V)')
    ax.set_ylim(-VDD * 0.1, VDD * 1.2)
    ax.set_title(f'4-bit Ideal ADC -> DAC  Ramp input  |  wall clock: {wall_s:.4f} s')
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.plot(t, vin, linewidth=1.0, color='C0', label='vin (ramp)')
    ax.set_ylabel('vin (V)')
    ax.set_ylim(-VDD * 0.1, VDD * 1.2)
    ax.legend(loc='upper left', fontsize=9)
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    ax2 = ax.twinx()
    l1, = ax.step(t, code, where='post', linewidth=1.0, color='C0', label='ADC code')
    l2, = ax2.plot(t, vout, linewidth=1.0, color='C1', linestyle='--', label='vout (DAC)')
    ax.set_ylabel('ADC code (0-15)')
    ax.set_ylim(-0.5, 15.5)
    ax.yaxis.set_major_locator(plt.MultipleLocator(4))
    ax2.set_ylabel('vout (V)')
    ax2.set_ylim(-0.5 * VSTEP, 15.5 * VSTEP)
    ax.set_xlabel('Time (ns)')
    ax.legend(handles=[l1, l2], loc='upper left', fontsize=9)
    ax.grid(True, alpha=0.3)

    axes[0].set_xlim(t[0], t[-1])
    fig.tight_layout()
    p = out_dir.parent / 'analyze_ramp.png'
    fig.savefig(str(p), dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"Plot saved: {p}")


def _plot_sine(out_dir: Path, wall_s: float, samples_per_cycle: int, fig_name: str) -> None:
    data = np.genfromtxt(out_dir / 'tran.csv', delimiter=',', names=True, dtype=None, encoding='utf-8')
    t_ns = data['time'] * 1e9
    post = t_ns > RST_END_SINE_NS
    t     = t_ns[post]
    vin   = data['vin'][post]
    vinsh = data['vin_sh'][post]
    vout  = data['vout'][post]
    code  = data['dout_code'].astype(float)[post]

    fig, axes = plt.subplots(3, 1, figsize=(12, 7), sharex=True,
                             gridspec_kw={'height_ratios': [2, 1.5, 2]})

    ax = axes[0]
    ax.plot(t, vin,   linewidth=1.0, label='vin (sine)')
    ax.plot(t, vinsh, linewidth=1.0, linestyle=':', color='C2', label='vin_sh (S&H)')
    ax.plot(t, vout,  linewidth=1.0, linestyle='--', label='vout (DAC)')
    ax.set_ylabel('Voltage (V)')
    ax.set_title(f'4-bit Ideal ADC -> DAC  Sine ({samples_per_cycle} samples/cycle)  |  wall clock: {wall_s:.4f} s')
    ax.legend(loc='upper right', fontsize=9)
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.step(t, code, where='post', linewidth=1.0, color='C2')
    ax.set_ylabel('dout_code')
    ax.set_ylim(-0.5, 15.5)
    ax.yaxis.set_major_locator(plt.MultipleLocator(4))
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    q_err_mv = (vinsh - vout) * 1e3
    ax.plot(t, q_err_mv, linewidth=1.0, color='C3')
    ax.axhline(0, color='k', linewidth=1.0, linestyle='--')
    lsb_mv = VSTEP * 1e3
    ax.axhline(-lsb_mv, color='gray', linewidth=1.0, linestyle=':', label=r'$\pm$1 LSB')
    ax.axhline( lsb_mv, color='gray', linewidth=1.0, linestyle=':')
    ax.set_ylabel('quant. error vin_sh-vout (mV)')
    ax.set_xlabel('Time (ns)')
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    axes[0].set_xlim(t[0], t[-1])
    fig.tight_layout()
    p = out_dir.parent / fig_name
    fig.savefig(str(p), dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"Plot saved: {p}")


def analyze(out_dir: Path = _DEFAULT_OUT) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Ramp ---
    out_ramp = out_dir / 'ramp'
    out_ramp.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    evas_simulate(str(HERE / 'tb_adc_dac_ideal_4b_ramp.scs'), output_dir=str(out_ramp))
    wall_ramp = time.perf_counter() - t0
    _plot_ramp(out_ramp, wall_ramp)

    # --- Sine (63 samples/cycle) ---
    out_sine = out_dir / 'sine'
    out_sine.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    evas_simulate(str(HERE / 'tb_adc_dac_ideal_4b_sine.scs'), output_dir=str(out_sine))
    wall_sine = time.perf_counter() - t0
    _plot_sine(out_sine, wall_sine, 63, 'analyze_sine.png')

    # --- Sine (1000 samples/cycle) ---
    out_sine1000 = out_dir / 'sine1000'
    out_sine1000.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    evas_simulate(str(HERE / 'tb_adc_dac_ideal_4b_sine1000.scs'), output_dir=str(out_sine1000))
    wall_sine1000 = time.perf_counter() - t0
    _plot_sine(out_sine1000, wall_sine1000, 1000, 'analyze_sine1000.png')


if __name__ == "__main__":
    analyze()
