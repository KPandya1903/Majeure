import { useEffect, useState } from "react";
import type { BacktestScenario } from "./api";
import { api } from "./api";
import { Chip } from "./components";

export default function Backtest() {
  const [data, setData] = useState<{ scenarios: BacktestScenario[]; note?: string } | null>(null);
  const [picked, setPicked] = useState<string | null>(null);

  useEffect(() => {
    api.backtest().then((d) => {
      setData(d);
      if (d.scenarios.length) setPicked(d.scenarios[0].scenario);
    });
  }, []);

  if (!data) return <div className="pg-card pg-empty">Loading…</div>;
  if (!data.scenarios.length) {
    return (
      <div className="pg-card pg-empty">
        {data.note || "Run "}<code>python src/backtest.py</code> to replay historical disruptions
        (Red Sea 2023, Baltimore 2024, US ports strike 2024, Panama drought 2023, Hormuz 2026).
      </div>
    );
  }

  const scenario = data.scenarios.find((s) => s.scenario === picked) ?? data.scenarios[0];

  return (
    <div>
      {data.note && <div className="pg-sub" style={{ padding: "0 0 12px" }}>{data.note}</div>}
      <table className="pg-table">
        <thead>
          <tr>
            <th>Scenario</th><th>Event</th><th>Matched</th><th>Traffic change</th>
            <th>Cheapest action</th><th>Cost waiting</th><th>Cost of best option</th><th>Recommended</th>
          </tr>
        </thead>
        <tbody>
          {data.scenarios.map((s) => (
            <tr key={s.scenario} className={s.scenario === picked ? "selected" : ""} onClick={() => setPicked(s.scenario)}>
              <td style={{ fontWeight: 600 }}>{s.scenario}</td>
              <td>{s.event}</td>
              <td>{s.matched ? <Chip text="Matched" tone="ok" /> : <Chip text="Not matched" tone="mute" />}</td>
              <td className="pg-num">{s.traffic_change_pct != null ? `${s.traffic_change_pct > 0 ? "+" : ""}${s.traffic_change_pct.toFixed(0)}%` : "—"}</td>
              <td>{s.cheapest_action}</td>
              <td className="pg-num">${s.exposure_wait_usd.toLocaleString()}</td>
              <td className="pg-num">${s.exposure_best_usd.toLocaleString()}</td>
              <td>{s.recommended_action}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {scenario && (
        <div className="pg-card" style={{ marginTop: 16 }}>
          <div className="pg-eyebrow">{scenario.scenario}</div>
          {scenario.summary && <div style={{ marginTop: 6, fontSize: 13.5, lineHeight: 1.55 }}>{scenario.summary}</div>}
          {scenario.traffic_series && scenario.traffic_series.length > 0 && (
            <>
              <div className="pg-fact-label" style={{ marginTop: 14 }}>
                {scenario.traffic_location}: daily {scenario.traffic_metric} around the event start
                {scenario.event_start ? ` (${scenario.event_start.slice(0, 10)})` : ""}, 7-day average
              </div>
              <svg viewBox="0 0 520 110" style={{ width: "100%", height: 120, marginTop: 8 }}>
                {(() => {
                  const series = scenario.traffic_series!;
                  const vals = series.map((d) => d.daily);
                  const avgVals = series.map((d) => d["7-day average"]);
                  const all = [...vals, ...avgVals];
                  const min = Math.min(...all), max = Math.max(...all) || 1;
                  const x = (i: number) => (i / (series.length - 1 || 1)) * 520;
                  const y = (v: number) => 100 - ((v - min) / (max - min || 1)) * 90;
                  const path = (arr: number[]) => arr.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
                  return (
                    <>
                      <path d={path(vals)} fill="none" stroke="var(--pg-faint)" strokeWidth={1.4} />
                      <path d={path(avgVals)} fill="none" stroke="var(--pg-blue)" strokeWidth={2.2} />
                    </>
                  );
                })()}
              </svg>
            </>
          )}
        </div>
      )}
    </div>
  );
}
