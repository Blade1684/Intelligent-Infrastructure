import statistics


def detect_service_failure(metric):
    """Classify container and application-health failures before trend analysis."""
    status = metric.get("status")

    if status in {"exited", "dead", "created", "paused"}:
        return {
            "anomaly": True,
            "type": "container_crash",
            "reason": f"Container status is {status}",
            "container_status": status
        }

    if status == "not_found":
        return {
            "anomaly": True,
            "type": "container_missing",
            "reason": "Container is not present"
        }

    if status == "running" and not metric.get("service_available", False):
        failure_type = "network_failure" if metric.get("health_error") else "service_unavailable"
        return {
            "anomaly": True,
            "type": failure_type,
            "reason": metric.get("health_error", "Health endpoint is unavailable"),
            "container_status": status
        }

    return {"anomaly": False, "reason": "Service is available"}


# ============================================================
# CPU ANOMALY DETECTION
# ============================================================

def detect_cpu_anomaly(history):
    """
    Detect significant CPU spikes using both:

    1. Statistical deviation
    2. Absolute CPU threshold

    Both conditions are required to reduce
    false positives.
    """

    if len(history) < 10:

        return {
            "anomaly": False,
            "reason": "Not enough historical data"
        }


    cpu_values = [

        sample.get(
            "cpu_percent",
            0
        )

        for sample in history

        if sample.get(
            "status"
        ) == "running"

    ]


    if len(cpu_values) < 10:

        return {
            "anomaly": False,
            "reason": "Not enough valid CPU samples"
        }


    current_cpu = cpu_values[-1]

    previous_values = cpu_values[:-1]


    mean_cpu = statistics.mean(
        previous_values
    )


    std_cpu = statistics.stdev(
        previous_values
    )


    # ========================================================
    # ABSOLUTE CPU THRESHOLD
    # ========================================================

    # Do not classify tiny CPU values as spikes,
    # even if their Z-score is statistically large.
    MINIMUM_SPIKE_CPU = 50


    if current_cpu < MINIMUM_SPIKE_CPU:

        return {

            "anomaly": False,

            "reason":
                "CPU below spike threshold",

            "current_cpu":
                current_cpu,

            "baseline":
                round(mean_cpu, 2)

        }


    # ========================================================
    # ZERO STANDARD DEVIATION
    # ========================================================

    if std_cpu == 0:

        if current_cpu > mean_cpu + 20:

            return {

                "anomaly": True,

                "type":
                    "cpu_spike",

                "current_cpu":
                    current_cpu,

                "baseline":
                    round(mean_cpu, 2),

                "z_score":
                    "infinite"

            }


        return {

            "anomaly": False,

            "reason":
                "CPU behaviour normal"

        }


    # ========================================================
    # Z-SCORE
    # ========================================================

    z_score = (

        current_cpu

        - mean_cpu

    ) / std_cpu


    if z_score > 3:

        return {

            "anomaly": True,

            "type":
                "cpu_spike",

            "current_cpu":
                current_cpu,

            "baseline":
                round(mean_cpu, 2),

            "z_score":
                round(z_score, 2)

        }


    return {

        "anomaly": False,

        "reason":
            "CPU behaviour normal",

        "current_cpu":
            current_cpu,

        "baseline":
            round(mean_cpu, 2),

        "z_score":
            round(z_score, 2)

    }


# ============================================================
# MEMORY ANOMALY DETECTION
# ============================================================

def detect_memory_anomaly(history):
    """
    Detect possible memory leaks by analysing
    sustained memory growth.
    """

    if len(history) < 10:

        return {

            "anomaly": False,

            "reason":
                "Not enough historical data"

        }


    memory_values = [

        sample.get(
            "memory_mb",
            0
        )

        for sample in history

        if sample.get(
            "status"
        ) == "running"

    ]


    if len(memory_values) < 10:

        return {

            "anomaly": False,

            "reason":
                "Not enough valid memory samples"

        }


    recent_memory = memory_values[-10:]


    increasing_count = sum(

        1

        for i in range(
            1,
            len(recent_memory)
        )

        if recent_memory[i]
        >
        recent_memory[i - 1]

    )


    growth = (

        recent_memory[-1]

        -

        recent_memory[0]

    )


    if (

        increasing_count >= 7

        and

        growth > 30

    ):

        return {

            "anomaly": True,

            "type":
                "memory_leak",

            "start_memory":
                recent_memory[0],

            "current_memory":
                recent_memory[-1],

            "growth_mb":
                round(growth, 2),

            "increasing_samples":
                increasing_count

        }


    return {

        "anomaly": False,

        "reason":
            "Memory behaviour normal",

        "current_memory":
            recent_memory[-1],

        "growth_mb":
            round(growth, 2),

        "increasing_samples":
            increasing_count

    }
