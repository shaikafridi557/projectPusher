import os
from flask import Blueprint, redirect, url_for
from flask_dance.contrib.github import make_github_blueprint, github

# Set environment variables (for development only; use .env file in production)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'  # Enables HTTP for OAuth
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'   # Loosen strict scope check

# Load values from environment or hardcode for local testing
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "Ov23liAth91COAnyVKDT")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "6bb70bf46e9bf656ac74f3e4920809fa627391d9")

# Create GitHub OAuth blueprint
github_blueprint = make_github_blueprint(
    client_id=GITHUB_CLIENT_ID,
    client_secret=GITHUB_CLIENT_SECRET,
    redirect_to="dashboard",  # Flask route name
    scope="repo"
)

# Create Blueprint for auth
auth = Blueprint('auth', __name__)

# Register GitHub blueprint with Flask app later in your main app
# app.register_blueprint(github_blueprint, url_prefix="/login")

@auth.route("/login/github")
def github_login():
    if not github.authorized:
        return redirect(url_for("github.login"))
    
    resp = github.get("/user")
    if not resp.ok:
        return "Failed to fetch user info from GitHub."

    github_info = resp.json()
    username = github_info.get("login")
    return f"Hello, {username}! You are successfully logged in with GitHub."
