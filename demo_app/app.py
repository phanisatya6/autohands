from __future__ import annotations

from flask import Flask, request

GATE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Acme Legacy Banking · Sign In</title>
<style>
  body { font-family: Georgia, serif; background: #ece9e0; color: #222; max-width: 640px; margin: 0 auto; padding: 24px; }
  .legacy-box { border: 3px double #666; background: #fdfcf7; padding: 24px; margin-top: 40px; }
  .legacy-btn { display: inline-block; padding: 10px 22px; border: 1px solid #444; background: #e8e4d8;
    color: #111; text-decoration: none; cursor: pointer; font: bold 14px Georgia, serif; letter-spacing: 1px; }
  #live-clock { display: none; }
</style></head><body>
<!-- legacy: a real <a> wedged to look like a button, plus a hidden tracking field -->
<form method="GET" action="{{ dash }}">
  <input type="hidden" name="sess" value="legacy-session" />
  <div class="legacy-box">
    <h2>Acme Legacy Banking</h2>
    <p>Welcome back. This classic terminal-style gate still works, one click at a time.</p>
    <div class="legacy-box">
      <span id="live-clock"></span>
      <a id="proceed" role="button" class="legacy-btn" href="{{ dash }}{{ fault_extra }}">Proceed</a>
    </div>
  </div>
</form>
</body></html>
"""

DASH_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Acme Legacy Banking · Dashboard</title>
<style>
  body { font-family: Georgia, serif; background: #ece9e0; color: #222; max-width: 700px; margin: 0 auto; padding: 24px; }
  h1, h2 { font-weight: normal; letter-spacing: 1px; }
  table { border-collapse: collapse; width: 100%; background: #fdfcf7; }
  th, td { border: 1px solid #777; padding: 8px 12px; text-align: left; font-size: 14px; }
  thead { background: #d8d4c6; }
  .pay { color: #0a4; text-decoration: none; font-weight: bold; }
  .legacy { color: #611; }
</style></head><body>
<h1>Dashboard</h1>
<table>
  <thead><tr><th>Account</th><th>Available Balance</th><th>Status</th><th></th></tr></thead>
  <tbody>
    <tr>
      <td>Zara (***1234)</td>
      <td data-key="available">{{ balance_cell }}</td>
      <td><span class="lbl">Status</span> Active</td>
      <td><a class="pay" href="/pay?acct=zara{{ decline_link }}">Pay Now</a></td>
    </tr>
  </tbody>
</table>
<p><em>Guest demo view. No real accounts, no real money.</em></p>
</body></html>
"""

PAY_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Acme Legacy Banking · Pay</title>
<style>
  body { font-family: Georgia, serif; background: #ece9e0; color: #222; max-width: 560px; margin: 0 auto; padding: 24px; }
  label { display: block; margin: 14px 0 4px; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }
  input, select { width: 100%; padding: 8px; border: 1px solid #777; background: #fffdf5; font-size: 15px; }
  .legacy-btn { display: inline-block; padding: 10px 22px; border: 1px solid #444; background: #e8e4d8;
    color: #111; text-decoration: none; cursor: pointer; font: bold 14px Georgia, serif; letter-spacing: 1px; margin-top: 20px; }
</style></head><body>
<h1>Make a Payment</h1>
<form method="GET" action="/result">
  <label for="amt">Amount (MYR)</label>
  <input id="amt" name="amt" type="text" placeholder="Amount" value="250.00" />
  <label for="from">From account</label>
  <select id="from" name="from"><option>Zara (***1234)</option></select>
  <label for="to">Payee</label>
  <input id="to" name="to" type="text" placeholder="Payee name" value="Utility Co." />
  <input type="hidden" name="decline" value="{{ decline }}" />
  <button class="legacy-btn" type="submit" name="act" value="pay">Authorize Payment</button>
</form>
<p>Authorizing is final.</p>
</body></html>
"""

RESULT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Acme Legacy Banking · Result</title>
<style>
  body { font-family: Georgia, serif; background: #ece9e0; color: #222; max-width: 600px; margin: 0 auto; padding: 24px; }
  .ok { border: 3px double #0a4; background: #eefaf0; padding: 18px; }
  .no { border: 3px double #a55; background: #fdf0f0; padding: 18px; }
</style></head><body>
<h1>Result</h1>
{{ result_html }}
<p><a href="/">Back to gate</a></p>
</body></html>
"""


def create_app() -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def gate():
        fault = request.args.get("fault", "")
        extra = ""
        extra_js = ""
        if fault == "busy":
            extra_js = "<script>setTimeout(function(){document.getElementById('proceed').style.visibility='hidden';},300);" \
                       "setTimeout(function(){document.getElementById('proceed').style.visibility='visible';},2600);</script>"
        fault_extra = f"?fault={fault}" if fault else ""
        html = GATE_HTML.replace("{{ dash }}", "/dashboard").replace("{{ fault_extra }}", fault_extra)
        return f"{html} {extra_js}"

    @app.route("/dashboard")
    def dashboard():
        fault = request.args.get("fault", "")
        unavailable = fault == "unavailable"
        balance = request.args.get("bal", "RM 1,250.00")
        decline_link = "&decline=1" if fault == "closed" else ""
        if unavailable:
            balance_cell = '<span class="legacy">Balance temporarily unavailable</span>'
        else:
            balance_cell = f'<span class="lbl">Available Balance</span> <strong>{balance}</strong>'
        return (
            DASH_HTML.replace("{{ balance_cell }}", balance_cell)
            .replace("{{ decline_link }}", decline_link)
        )

    @app.route("/pay")
    def pay():
        decline = "1" if request.args.get("decline") == "1" else ""
        return PAY_HTML.replace("{{ decline }}", decline)

    @app.route("/result")
    def result():
        declined = request.args.get("decline") == "1"
        to = request.args.get("to", "Utility Co.")
        import uuid

        ref = "MYR-" + uuid.uuid4().hex[:8].upper()
        if declined:
            result_html = '<div class="no"><p><strong>Payment declined:</strong> account closed (demo business outcome).</p></div>'
        else:
            result_html = f'<div class="ok"><p><strong>Payment sent</strong> to {to}. Reference {ref}.</p></div>'
        return RESULT_HTML.replace("{{ result_html }}", result_html).replace("{{ to }}", to).replace(
            "{{ ref }}", ref
        )

    return app


def start(port: int = 5007) -> Flask:
    app = create_app()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    return app


if __name__ == "__main__":
    start()