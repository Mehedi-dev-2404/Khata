from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Khata", description="WhatsApp-native bookkeeping agent")


@app.get("/health")
def health():
    from database import get_client
    db_ok = get_client() is not None
    return {"status": "ok", "db_connected": db_ok}


@app.get("/ledger", response_class=HTMLResponse)
def ledger_view():
    from ledger_csv import HEADERS, _read_all_rows
    rows = _read_all_rows()

    direction_badge = {
        "owed_to_business": ('<span style="color:#1a7f37;font-weight:600">'
                             '&#8593; owed to us</span>'),
        "paid_by_business": ('<span style="color:#cf222e;font-weight:600">'
                             '&#8595; paid out</span>'),
    }

    header_cells = "".join(f"<th>{h}</th>" for h in HEADERS)

    body_rows = ""
    for row in rows:
        cells = ""
        for h in HEADERS:
            val = row.get(h, "")
            if h == "Direction":
                val = direction_badge.get(val, val)
            elif h == "Confidence":
                try:
                    pct = int(float(val) * 100)
                    bar_color = "#1a7f37" if pct >= 70 else "#bf8700"
                    val = (f'<div style="display:flex;align-items:center;gap:6px">'
                           f'<div style="width:60px;background:#eee;border-radius:4px;height:8px">'
                           f'<div style="width:{pct}%;background:{bar_color};'
                           f'border-radius:4px;height:8px"></div></div>'
                           f'<span>{pct}%</span></div>')
                except (ValueError, TypeError):
                    pass
            cells += f"<td>{val}</td>"
        body_rows += f"<tr>{cells}</tr>"

    if not rows:
        body_rows = f'<tr><td colspan="{len(HEADERS)}" style="text-align:center;color:#888;padding:2rem">No transactions yet.</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Khata — Ledger</title>
<style>
  body {{font-family:system-ui,sans-serif;margin:0;padding:1.5rem;background:#f6f8fa;color:#1f2328}}
  h1 {{margin:0 0 1rem;font-size:1.4rem}}
  .meta {{font-size:.85rem;color:#656d76;margin-bottom:1rem}}
  table {{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;
          box-shadow:0 1px 3px rgba(0,0,0,.1);overflow:hidden}}
  th {{background:#f0f6ff;padding:.6rem .8rem;text-align:left;font-size:.8rem;
       text-transform:uppercase;letter-spacing:.05em;color:#0969da;border-bottom:1px solid #d0d7de}}
  td {{padding:.55rem .8rem;font-size:.875rem;border-bottom:1px solid #f0f0f0;vertical-align:middle}}
  tr:last-child td {{border-bottom:none}}
  tr:hover td {{background:#f6f8fa}}
</style>
</head>
<body>
<h1>Khata — Ledger</h1>
<div class="meta">{len(rows)} transaction(s) &mdash; <a href="/ledger">refresh</a></div>
<table>
  <thead><tr>{header_cells}</tr></thead>
  <tbody>{body_rows}</tbody>
</table>
</body>
</html>"""
    return HTMLResponse(content=html)
