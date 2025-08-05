import os
from collections import defaultdict
from flask import Flask, redirect, url_for, render_template, session, request, flash, jsonify
from flask_dance.contrib.github import make_github_blueprint, github
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from datetime import datetime
from utils.repo_utils import move_or_copy_item
import uuid
# --- NEW IMPORTS ---
import threading # To run the worker in the background
from worker import process_jobs # Import the worker's main function

# --- NEW IMPORTS FOR MONGODB ---
from flask_pymongo import PyMongo

# --- YOUR EXISTING UTILS IMPORTS ---
from utils.repo_utils import (
    create_repo_from_zip, 
    get_user_repos, 
    get_repo_contents, 
    get_file_content, 
    update_file_in_repo,
    create_new_file,
    create_new_folder,
    delete_repo,
    get_repo_stats,
    get_repo_languages
)

# This setting is the final fix for the OAuth scope warning
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

# Load environment variables from a .env file
load_dotenv()

app = Flask(__name__)

# --- App Configuration (Updated for MongoDB) ---
app.config.update(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY"),
    SESSION_COOKIE_SAMESITE='Lax',
    # --- NEW: MONGODB CONFIGURATION ---
    # This reads the connection string from your .env file
    MONGO_URI=os.environ.get("MONGO_URI")
)
# This is required for running behind a reverse proxy (common in production).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

if not app.config["SECRET_KEY"]:
    raise ValueError("FLASK_SECRET_KEY is not set in your environment.")
if not app.config["MONGO_URI"]:
    raise ValueError("MONGO_URI is not set in your environment.")

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# --- NEW: MONGODB SETUP ---
mongo = PyMongo(app)

# --- GitHub OAuth Blueprint (Unchanged) ---
github_bp = make_github_blueprint(
    client_id=os.environ.get("GITHUB_CLIENT_ID"),
    client_secret=os.environ.get("GITHUB_CLIENT_SECRET"),
    scope=["repo", "delete_repo"],
    redirect_to="github_login"
)
app.register_blueprint(github_bp, url_prefix="/login")
print("Starting background worker thread...")
worker_thread = threading.Thread(target=process_jobs, daemon=True)
worker_thread.start()

# --- Standard Application Routes (Unchanged) ---

@app.route("/")
def home():
    if not github.authorized:
        return render_template("login.html")
    return redirect(url_for("dashboard"))

@app.route("/github_login")
def github_login():
    if not github.authorized:
        flash("Authorization with GitHub failed. Please try again.", "error")
        return redirect(url_for("home"))
        
    resp = github.get("/user")
    if not resp.ok:
        flash("Failed to fetch your user information from GitHub.", "error")
        return redirect(url_for("home"))

    session["github_user"] = resp.json()
    return redirect(url_for("dashboard"))

@app.route("/api/repo/<repo_name>/move", methods=["POST"])
def move_item_in_repo(repo_name):
    """
    API endpoint to handle moving or copying a SINGLE item or a LIST of items.
    """
    if not github.authorized or "github_user" not in session:
        return jsonify({"success": False, "error": "Not authorized"}), 401

    data = request.get_json()
    source_path = data.get("source_path") # This can now be a string OR a list of strings
    destination_path = data.get("destination_path")
    operation = data.get("operation")

    if not all([source_path, operation]):
        return jsonify({"success": False, "error": "Missing source path or operation type."}), 400

    access_token = github.token["access_token"]
    owner = session["github_user"]["login"]

    # If the source_path is not a list, make it a list with one item for consistent processing
    source_paths = source_path if isinstance(source_path, list) else [source_path]

    # Loop through each source path and perform the operation
    for path in source_paths:
        result = move_or_copy_item(
            token=access_token,
            owner=owner,
            repo_name=repo_name,
            source_path=path,
            destination_path=destination_path,
            operation=operation
        )
        # If any single operation fails, stop and return the error immediately
        if not result["success"]:
            return jsonify(result)

    # If all operations in the loop succeeded
    return jsonify({"success": True})
 
 
@app.route("/dashboard")
def dashboard():
    # 1. If the user isn't authorized at all, send them to the login page.
    if not github.authorized:
        return redirect(url_for("home"))

    # 2. If authorized, but user data is not in the session, try to fetch it.
    if "github_user" not in session:
        resp = github.get("/user")
        if resp.ok:
            # If the fetch is successful, store the data in the session.
            session["github_user"] = resp.json()
        else:
            # If fetching fails (e.g., token expired), clear the session and send to login.
            session.clear()
            flash("Could not fetch your GitHub profile. Please log in again.", "error")
            return redirect(url_for("home"))

    # 3. Now that we are sure the user data is in the session, proceed as normal.
    access_token = github.token["access_token"]
    repos = get_user_repos(access_token)
    
    return render_template("dashboard.html", user=session["github_user"], repos=repos)


# --- MODIFIED /upload ROUTE FOR MONGODB ---
@app.route("/upload", methods=["POST"])
def upload():
    if "github_user" not in session: 
        return redirect(url_for("home"))
        
    project_file = request.files.get("project")
    repo_name = request.form.get("repo_name")
    
    # --- NEW: Get the checkbox value for private repository ---
    is_private = request.form.get('is_private') == 'true'
    
    if not project_file or not repo_name:
        flash("Missing project file or repository name.", "error")
        return redirect(url_for("dashboard"))
        
    if not project_file.filename.lower().endswith('.zip'):
        flash("Invalid file type. Please upload a .zip file.", "error")
        return redirect(url_for("dashboard"))

    access_token = github.token["access_token"]
    
    temp_dir = os.path.join(app.root_path, 'tmp', 'uploads')
    os.makedirs(temp_dir, exist_ok=True)
    filename = secure_filename(f"{session['github_user']['login']}_{repo_name}_{os.urandom(4).hex()}.zip")
    temp_filepath = os.path.join(temp_dir, filename)
    project_file.save(temp_filepath)

    job_id = str(uuid.uuid4())
    job_document = {
        "_id": job_id,
        "status": "queued",
        "created_at": datetime.utcnow(),
        "access_token": access_token,
        "temp_filepath": temp_filepath,
        "repo_name": repo_name,
        "is_private": is_private, # <-- NEW: Save the privacy setting in the job
        "result": None
    }
    mongo.db.jobs.insert_one(job_document)

    return redirect(url_for("upload_status", job_id=job_id))


# --- NEW ROUTES FOR MONGODB STATUS CHECKING ---

@app.route("/upload/status/<job_id>")
def upload_status(job_id):
    """Renders a user-facing page that will poll for the job's status."""
    return render_template("upload_status.html", job_id=job_id)

# --- MODIFIED API ENDPOINT ---
@app.route("/api/upload/status/<job_id>")
def api_upload_status(job_id):
    """Provides the status and progress of a background job from MongoDB."""
    # Project only the fields we need
    job = mongo.db.jobs.find_one(
        {"_id": job_id},
        {"_id": 0, "status": 1, "result": 1, "progress": 1} 
    )
    if job:
        response_data = {
            'status': job['status'],
            'result': job.get('result'),
            'progress': job.get('progress') # <-- ADD THIS LINE
        }
    else:
        response_data = {'status': 'not_found'}

    return jsonify(response_data)
# --- API, IDE, and Repo Management Routes (Unchanged) ---

@app.route("/api/dashboard_analytics")
def dashboard_analytics():
    if not github.authorized or "github_user" not in session: return jsonify({"error": "Not authorized"}), 401
    access_token = github.token["access_token"]
    owner = session["github_user"]["login"]
    repos = get_user_repos(access_token)
    if not repos: return jsonify({"total_stars": 0, "language_stats": {}, "top_language": "N/A", "commit_history": [0]*52})
    total_stars = sum(repo.get('stargazers_count', 0) for repo in repos)
    language_stats = defaultdict(int)
    for repo in repos:
        if repo.get("language"): language_stats[repo.get("language")] += 1
    sorted_languages = sorted(language_stats.items(), key=lambda item: item[1], reverse=True)
    top_language = sorted_languages[0][0] if sorted_languages else "N/A"
    top_5_languages = dict(sorted_languages[:5])
    other_count = sum(count for lang, count in sorted_languages[5:])
    if other_count > 0: top_5_languages['Other'] = other_count
    commit_history = [0] * 52
    recent_repos = sorted(repos, key=lambda x: x['updated_at'], reverse=True)[:5]
    for repo in recent_repos:
        stats_result = get_repo_stats(access_token, owner, repo['name'], 'participation')
        if stats_result["success"] and stats_result["data"]:
            user_commits = stats_result["data"].get("owner", [])
            for i, count in enumerate(user_commits):
                if i < 52: commit_history[i] += count
    return jsonify({"total_stars": total_stars, "language_stats": top_5_languages, "top_language": top_language, "commit_history": commit_history})

@app.route("/repo/<repo_name>/", defaults={'folder_path': ''})
@app.route("/repo/<repo_name>/<path:folder_path>")
def view_repository(repo_name, folder_path):
    if not github.authorized or "github_user" not in session: return redirect(url_for("home"))
    access_token, owner = github.token["access_token"], session["github_user"]["login"]
    result = get_repo_contents(access_token, owner, repo_name, path=folder_path)
    if result.get("success"): return render_template("repository_view.html", repo_name=repo_name, contents=result["contents"], path=folder_path, breadcrumbs=folder_path.split('/') if folder_path else [])
    else: return render_error_page(result.get("error"))

@app.route("/repo/<repo_name>/edit/<path:file_path>")
def edit_file(repo_name, file_path):
    if not github.authorized or "github_user" not in session: return redirect(url_for("home"))
    access_token, owner = github.token["access_token"], session["github_user"]["login"]
    result = get_file_content(access_token, owner, repo_name, file_path)
    if result.get("success"): return render_template("file_editor.html", repo_name=repo_name, file_path=file_path, content=result["content"], sha=result["sha"], is_binary=result["is_binary"])
    else: return render_error_page(result.get("error"))

@app.route("/repo/<repo_name>/save/<path:file_path>", methods=["POST"])
def save_file(repo_name, file_path):
    if not github.authorized or "github_user" not in session: return redirect(url_for("home"))
    access_token, owner = github.token["access_token"], session["github_user"]["login"]
    new_content, commit_message, sha = request.form.get("content"), request.form.get("commit_message"), request.form.get("sha")
    if new_content is None or not commit_message or not sha: return render_error_page("Missing content, commit message, or file SHA for saving.")
    result = update_file_in_repo(access_token, owner, repo_name, file_path, new_content, commit_message, sha)
    if result.get("success"):
        flash(f"Successfully committed '{file_path}'!", "success")
        return redirect(url_for("view_repository", repo_name=repo_name, folder_path=os.path.dirname(file_path)))
    else: return render_error_page(result.get("error"))

@app.route("/repo/<repo_name>/new_file", methods=["POST"])
def new_file(repo_name):
    if not github.authorized or "github_user" not in session: return redirect(url_for("home"))
    access_token, owner = github.token["access_token"], session["github_user"]["login"]
    current_path, new_file_name = request.form.get("current_path", ""), request.form.get("file_name")
    if not new_file_name: return render_error_page("New file name cannot be empty.")
    full_path = os.path.join(current_path, new_file_name).replace("\\", "/")
    result = create_new_file(access_token, owner, repo_name, full_path, f"feat: Create new file '{new_file_name}'")
    if result.get("success"):
        flash(f"Successfully created file '{new_file_name}'!", "success")
        return redirect(url_for("edit_file", repo_name=repo_name, file_path=full_path))
    else: return render_error_page(result.get("error"))

@app.route("/repo/<repo_name>/new_folder", methods=["POST"])
def new_folder(repo_name):
    if not github.authorized or "github_user" not in session: return redirect(url_for("home"))
    access_token, owner = github.token["access_token"], session["github_user"]["login"]
    current_path, new_folder_name = request.form.get("current_path", ""), request.form.get("folder_name")
    if not new_folder_name: return render_error_page("New folder name cannot be empty.")
    full_path = os.path.join(current_path, new_folder_name).replace("\\", "/")
    result = create_new_folder(access_token, owner, repo_name, full_path)
    if result.get("success"):
        flash(f"Successfully created folder '{new_folder_name}'!", "success")
        return redirect(url_for("view_repository", repo_name=repo_name, folder_path=full_path))
    else: return render_error_page(result.get("error"))

@app.route("/delete_repo/<repo_name>", methods=["POST"])
def delete_repo_route(repo_name):
    if not github.authorized or "github_user" not in session: return redirect(url_for("home"))
    access_token, owner = github.token["access_token"], session["github_user"]["login"]
    result = delete_repo(access_token, owner, repo_name)
    if result.get("success"): flash(f"Repository '{repo_name}' has been permanently deleted.", "success")
    else: flash(f"Error deleting repository '{repo_name}': {result.get('error')}", "error")
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been successfully logged out.", "info")
    return redirect(url_for("home"))


# --- HTML Rendering Helper Functions (Unchanged) ---

def render_success_page(repo_url, repo_name):
    """Renders a professional success page with modern styling."""
    return f'''
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Upload Successful</title><link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet"><style>body{{font-family:'Inter',sans-serif;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;}}.success-container{{background:rgba(255,255,255,0.95);border-radius:24px;padding:60px 40px;text-align:center;box-shadow:0 25px 50px rgba(0,0,0,0.15);max-width:500px;width:100%;}}.success-icon{{width:80px;height:80px;background:linear-gradient(135deg,#10b981,#059669);border-radius:50%;display:flex;align-items:center;justify-content:center;margin:0 auto 30px;}}.success-icon i{{font-size:36px;color:white;}}.success-title{{font-size:28px;font-weight:700;color:#1f2937;margin-bottom:16px;}}.success-message{{font-size:16px;color:#6b7280;margin-bottom:40px;}}.repo-card{{background:#f8fafc;border:2px solid #e5e7eb;border-radius:16px;padding:24px;margin-bottom:40px;}}.repo-name{{font-size:18px;font-weight:600;color:#1f2937;}}.btn{{padding:14px 28px;border-radius:12px;text-decoration:none;font-weight:600;font-size:14px;display:inline-flex;align-items:center;gap:8px;}}.btn-primary{{background:linear-gradient(135deg,#6366f1,#4f46e5);color:white;}}.btn-secondary{{background:white;color:#6b7280;border:2px solid #e5e7eb;}}</style></head><body><div class="success-container"><div class="success-icon"><i class="fas fa-check"></i></div><h1 class="success-title">Upload Successful!</h1><p class="success-message">Your project is now live in your new GitHub repository.</p><div class="repo-card"><div class="repo-name"><i class="fab fa-github"></i> {repo_name}</div></div><div style="display:flex;gap:16px;justify-content:center;"><a href="{repo_url}" class="btn btn-primary" target="_blank"><i class="fas fa-external-link-alt"></i> View on GitHub</a><a href="/dashboard" class="btn btn-secondary"><i class="fas fa-arrow-left"></i> To Dashboard</a></div></div></body></html>
    '''

def render_error_page(error_message):
    """Renders a professional error page with modern styling."""
    return f'''
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Upload Failed</title><link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet"><style>body{{font-family:'Inter',sans-serif;background:linear-gradient(135deg,#ef4444 0%,#dc2626 100%);display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;}}.error-container{{background:rgba(255,255,255,0.95);border-radius:24px;padding:60px 40px;text-align:center;box-shadow:0 25px 50px rgba(0,0,0,0.15);max-width:500px;width:100%;}}.error-icon{{width:80px;height:80px;background:linear-gradient(135deg,#ef4444,#dc2626);border-radius:50%;display:flex;align-items:center;justify-content:center;margin:0 auto 30px;}}.error-icon i{{font-size:36px;color:white;}}.error-title{{font-size:28px;font-weight:700;color:#1f2937;margin-bottom:16px;}}.error-message{{font-size:16px;color:#6b7280;margin-bottom:40px;padding:20px;background:#fef2f2;border-radius:12px;border-left:4px solid #ef4444;text-align:left;}}.btn{{padding:14px 28px;border-radius:12px;text-decoration:none;font-weight:600;font-size:14px;display:inline-flex;align-items:center;gap:8px;background:linear-gradient(135deg,#6366f1,#4f46e5);color:white;}}</style></head><body><div class="error-container"><div class="error-icon"><i class="fas fa-exclamation-triangle"></i></div><h1 class="error-title">An Error Occurred</h1><div class="error-message"><strong>Details:</strong> {error_message}</div><a href="/dashboard" class="btn"><i class="fas fa-arrow-left"></i> Back to Dashboard</a></div></body></html>
    '''

# --- Main Execution ---
if __name__ == '__main__':
    app.run(debug=True, port=5000)
