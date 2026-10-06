"""In Home Display Plugin (Lite variant).

Frontend-only plugin — this task just stays alive so the plugin shows
as running.  All functionality is in frontend/index.html, served at
/plugins/in-home-display/, which polls /api/inverter/data and reads
/api/plugins/in-home-display/settings directly.
"""


async def run(ctx):
    ctx.log("in-home-display started (frontend-only plugin)")
    while True:
        await ctx.sleep_ms(60000)


async def stop(ctx):
    ctx.log("in-home-display stopped")
