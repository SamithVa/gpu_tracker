import socket


def usage_totals(db, since, host="all", *, until=None):
    return db.execute(
        """
        SELECT host, username,
               SUM(gpus * (MIN(end, COALESCE(:until, end)) - MAX(start, :since))) / 3600 AS gpu_hours,
               SUM(memory_mib * (MIN(end, COALESCE(:until, end)) - MAX(start, :since))) / 3686400 AS gib_hours
        FROM (
            SELECT 'local' AS host, username, start, end, gpus, memory_mib
            FROM intervals JOIN usage USING (start)
            WHERE :host IN ('local', 'all') AND end > :since AND (:until IS NULL OR start < :until)
            UNION ALL
            SELECT host, username, start, end, gpus, memory_mib
            FROM remote_intervals JOIN remote_usage USING (host, start)
            WHERE (:host = 'all' OR host = :host) AND end > :since AND (:until IS NULL OR start < :until)
        )
        GROUP BY host, username
        ORDER BY gpu_hours DESC, gib_hours DESC, username, host
        """,
        {"since": since, "until": until, "host": host},
    ).fetchall()


def server_label(host):
    return socket.gethostname() if host == "local" else host
