import { MissionControl } from "@/components/MissionControl";
import { loadBundle } from "@/lib/data";

export const dynamic = "force-dynamic";

export default async function Page() {
  const bundle = await loadBundle();

  if (!bundle) {
    return (
      <main className="mx-auto max-w-2xl p-10">
        <h1 className="text-lg font-semibold text-ink">Mishkah · Mission Control</h1>
        <p className="mt-3 text-[13px] text-ink">
          No data bundle found. The UI renders only what the worker computed — generate the bundle first:
        </p>
        <pre className="mt-4 overflow-x-auto rounded border border-line bg-panel p-3 text-2xs text-cyan">
          {`# from the repository root
.venv/bin/python -m worker demo seed          # synthetic dataset, if the DB is empty
.venv/bin/python -m worker backtest run --split discovery
.venv/bin/python -m worker report build --split discovery
.venv/bin/python -m worker ui export --split discovery --days 45 --clean
npm --prefix ui run dev                       # then open http://localhost:3000`}
        </pre>
        <p className="mt-4 text-2xs text-dim">
          Phase 1 is research + paper trading only; there is no code path in this repository that places a real order.
        </p>
      </main>
    );
  }

  return <MissionControl bundle={bundle} />;
}
