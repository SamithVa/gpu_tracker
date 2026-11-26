<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="30">
    <title>User rankings · gpuview</title>
    <style>
        :root { color-scheme: dark; font: 16px/1.5 system-ui, sans-serif; }
        body { margin: 2rem auto; max-width: 54rem; padding: 0 1rem; }
        a { color: #a1ceff; }
        nav { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; }
        .views { gap: 1rem; padding-bottom: 1rem; border-bottom: 1px solid #50565e; }
        .brand { margin-inline-end: .5rem; }
        .periods a { padding: .4rem .75rem; border: 1px solid #50565e; border-radius: .4rem; text-decoration: none; }
        .periods [aria-current] { background: #a1ceff; border-color: #a1ceff; color: #102135; font-weight: 700; }
        .filters { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: .75rem; }
        select, button { font: inherit; padding: .35rem; }
        .table-scroll { overflow-x: auto; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border-bottom: 1px solid #50565e; padding: .65rem .5rem; text-align: left; }
        .numeric { text-align: right; }
        td { font-variant-numeric: tabular-nums; }
        .detail { color: #b9c0c8; }
    </style>
</head>
<body>
    <header>
        <nav class="views" aria-label="Dashboard views">
            <strong class="brand">gpuview</strong>
            <span aria-current="page">User rankings</span>
            <a href="/live">Live GPUs</a>
        </nav>
    </header>
    <main>
    <h1>GPU usage by user</h1>
    <div class="filters">
    <nav class="periods" aria-label="Reporting period">
        % for window, label in (("24h", "Last 24 hours"), ("7d", "Last 7 days"), ("30d", "Last 30 days")):
        <a href="?period={{window}}&amp;host={{host}}" {{!'aria-current="true"' if window == period else ''}}>{{label}}</a>
        % end
    </nav>
    <form method="get">
        <input type="hidden" name="period" value="{{period}}">
        <label for="host">Server</label>
        <select name="host" id="host">
            <option value="all" {{'selected' if host == 'all' else ''}}>All servers</option>
            % for value, label in hosts.items():
            <option value="{{value}}" {{'selected' if host == value else ''}}>{{label}}</option>
            % end
        </select>
        <button type="submit">Apply</button>
    </form>
    </div>
    <p class="detail">
        {{round(covered / 3600, 2)}} of {{target_hours}} {{unit}} sampled.
        % if last_sample:
        Latest sample {{last_sample.strftime('%Y-%m-%d %H:%M:%S %Z')}}.
        % end
    </p>
    % if stale:
    <p role="status">Waiting for a recent GPU sample.</p>
    % end
    % if users:
    <div class="table-scroll">
    <table>
        <thead><tr><th scope="col">Rank</th><th scope="col">Linux user</th><th scope="col">Server</th><th class="numeric" scope="col" aria-sort="descending">GPU-hours occupied</th><th class="numeric" scope="col">VRAM (GiB-hours)</th></tr></thead>
        <tbody>
        % for rank, (server, user, gpu_hours, gib_hours) in enumerate(users, start=1):
        <tr><td>{{rank}}</td><th scope="row">{{'Unknown owner' if user == '?' else user}}</th><td>{{hosts[server]}}</td><td class="numeric">{{f'{gpu_hours:.3f}'}}</td><td class="numeric">{{f'{gib_hours:.2f}'}}</td></tr>
        % end
        </tbody>
    </table>
    </div>
    % elif covered:
    <p>No GPU processes observed in this period.</p>
    % else:
    <p>Usage totals appear after the first complete sampling interval.</p>
    % end
    <p class="detail">One GPU held for one hour counts as one occupied GPU-hour. The collector samples every 30 seconds and includes idle processes that still hold a GPU context. Shared GPUs count for each user.</p>
    </main>
</body>
</html>
