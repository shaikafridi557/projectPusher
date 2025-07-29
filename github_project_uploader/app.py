from flask import Flask, redirect, url_for, render_template, session, request
from flask_dance.contrib.github import make_github_blueprint, github
from utils.repo_utils import create_repo_from_zip, get_user_repos
from dotenv import load_dotenv
import os

# Step 1: Load all the secret keys from your .env file
load_dotenv()

# Step 2: Create the Flask application
app = Flask(__name__)

# Step 3: Set a permanent secret key for the session
# This is a critical fix to ensure your login session is not lost after a server restart.
app.secret_key = os.environ.get("FLASK_SECRET_KEY")

# Check if the secret key was loaded correctly. The app cannot run without it.
if not app.secret_key:
    raise ValueError("No FLASK_SECRET_KEY set in the .env file. Please generate one and add it.")

# Allow the app to work on http:// for local development
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Step 4: Configure the GitHub OAuth login
github_bp = make_github_blueprint(
    client_id=os.environ.get("GITHUB_CLIENT_ID"),
    client_secret=os.environ.get("GITHUB_CLIENT_SECRET"),
    # The "scope" tells GitHub what permissions we need. "repo" gives access to repositories.
    scope="repo",
    # After login, GitHub will send the user to the 'github_login' route below.
    redirect_to="github_login"
)
app.register_blueprint(github_bp, url_prefix="/login")


# --- Application Routes ---

@app.route("/")
def home():
    """The main page. It shows the login button or redirects to the dashboard."""
    if not github.authorized:
        # If the user is not logged in, show them the login.html page
        return render_template("login.html")
    
    # If the user is already logged in, send them directly to the dashboard
    return redirect(url_for("dashboard"))

@app.route("/github_login")
def github_login():
    """This route is called by GitHub after a successful login."""
    if not github.authorized:
        # If for some reason the login failed, show an error.
        return "GitHub login failed.", 401

    # Fetch the user's GitHub profile information
    resp = github.get("/user")
    if not resp.ok:
        return "Failed to fetch user information from GitHub.", 401

    # Store the user's profile in the session so we remember them
    session["github_user"] = resp.json()
    
    # Redirect to the main dashboard page
    return redirect(url_for("dashboard"))

@app.route("/dashboard")
def dashboard():
    """The main dashboard page, shown after a successful login."""
    # Protect this page: if the user isn't logged in, send them back to the home page.
    if not github.authorized or "github_user" not in session:
        return redirect(url_for("home"))

    # Get the user's access token from the session and fetch their repos
    access_token = github.token["access_token"]
    repos = get_user_repos(access_token)
    
    # Render the index.html template with the user's info and list of repos
    return render_template("dashboard.html", user=session["github_user"], repos=repos)

#
# --- THIS IS THE FULLY CORRECTED UPLOAD FUNCTION ---
#
@app.route("/upload", methods=["POST"])
def upload():
    """Handles the project zip file upload and repository creation."""
    # Protect this route: user must be logged in.
    if "github_user" not in session:
        return redirect(url_for("home"))
    
    # Get the file and the repository NAME from the HTML form.
    project_file = request.files.get("project")
    repo_name = request.form.get("repo_name")
    
    # Basic validation to make sure we received the file and name.
    if not project_file or not repo_name:
        return render_error_page("Missing project zip file or repository name.")
    
    if not project_file.filename.lower().endswith('.zip'):
        return render_error_page("Invalid file type. Please upload a .zip file.")
    
    # Get the user's access token to authorize the API call.
    access_token = github.token["access_token"]
    
    # Call the function with arguments in the correct order:
    result = create_repo_from_zip(access_token, project_file, repo_name)
    
    # Display a success or failure message based on the result from the function.
    if result.get("success"):
        return render_success_page(result['repo_url'], repo_name)
    else:
        return render_error_page(result.get('error'))


def render_success_page(repo_url, repo_name):
    """Renders a professional success page with modern styling."""
    return f'''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Upload Successful - ProjectPusher</title>
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}

            body {{
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 20px;
            }}

            .success-container {{
                background: rgba(255, 255, 255, 0.95);
                backdrop-filter: blur(20px);
                border-radius: 24px;
                padding: 60px 40px;
                text-align: center;
                box-shadow: 0 25px 50px rgba(0, 0, 0, 0.15);
                max-width: 500px;
                width: 100%;
                position: relative;
                overflow: hidden;
            }}

            .success-container::before {{
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 4px;
                background: linear-gradient(90deg, #10b981, #059669, #047857);
            }}

            .success-icon {{
                width: 80px;
                height: 80px;
                background: linear-gradient(135deg, #10b981, #059669);
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                margin: 0 auto 30px;
                animation: successPulse 2s ease-in-out infinite;
            }}

            .success-icon i {{
                font-size: 36px;
                color: white;
            }}

            @keyframes successPulse {{
                0%, 100% {{ transform: scale(1); }}
                50% {{ transform: scale(1.05); }}
            }}

            .success-title {{
                font-size: 28px;
                font-weight: 700;
                color: #1f2937;
                margin-bottom: 16px;
                line-height: 1.2;
            }}

            .success-message {{
                font-size: 16px;
                color: #6b7280;
                margin-bottom: 40px;
                line-height: 1.6;
            }}

            .repo-card {{
                background: #f8fafc;
                border: 2px solid #e5e7eb;
                border-radius: 16px;
                padding: 24px;
                margin-bottom: 40px;
                transition: all 0.3s ease;
                position: relative;
            }}

            .repo-card:hover {{
                border-color: #6366f1;
                transform: translateY(-2px);
                box-shadow: 0 10px 25px rgba(99, 102, 241, 0.15);
            }}

            .github-logo {{
                width: 32px;
                height: 32px;
                margin-bottom: 12px;
            }}

            .repo-name {{
                font-size: 18px;
                font-weight: 600;
                color: #1f2937;
                margin-bottom: 8px;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 8px;
            }}

            .repo-url {{
                color: #6366f1;
                text-decoration: none;
                font-size: 14px;
                word-break: break-all;
                transition: color 0.3s ease;
            }}

            .repo-url:hover {{
                color: #4f46e5;
            }}

            .external-link-icon {{
                font-size: 12px;
                margin-left: 4px;
                opacity: 0.7;
            }}

            .action-buttons {{
                display: flex;
                gap: 16px;
                justify-content: center;
                flex-wrap: wrap;
            }}

            .btn {{
                padding: 14px 28px;
                border-radius: 12px;
                text-decoration: none;
                font-weight: 600;
                font-size: 14px;
                transition: all 0.3s ease;
                display: inline-flex;
                align-items: center;
                gap: 8px;
                min-width: 140px;
                justify-content: center;
            }}

            .btn-primary {{
                background: linear-gradient(135deg, #6366f1, #4f46e5);
                color: white;
                border: none;
            }}

            .btn-primary:hover {{
                transform: translateY(-2px);
                box-shadow: 0 10px 25px rgba(99, 102, 241, 0.3);
            }}

            .btn-secondary {{
                background: white;
                color: #6b7280;
                border: 2px solid #e5e7eb;
            }}

            .btn-secondary:hover {{
                border-color: #d1d5db;
                color: #374151;
                transform: translateY(-1px);
            }}

            .floating-elements {{
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                pointer-events: none;
                overflow: hidden;
                z-index: -1;
            }}

            .floating-elements i {{
                position: absolute;
                color: rgba(255, 255, 255, 0.1);
                animation: float 6s ease-in-out infinite;
            }}

            .floating-elements i:nth-child(1) {{
                top: 20%;
                left: 10%;
                font-size: 24px;
                animation-delay: 0s;
            }}

            .floating-elements i:nth-child(2) {{
                top: 60%;
                right: 15%;
                font-size: 18px;
                animation-delay: 2s;
            }}

            .floating-elements i:nth-child(3) {{
                bottom: 20%;
                left: 20%;
                font-size: 20px;
                animation-delay: 4s;
            }}

            @keyframes float {{
                0%, 100% {{ transform: translateY(0px) rotate(0deg); }}
                50% {{ transform: translateY(-20px) rotate(10deg); }}
            }}

            @media (max-width: 640px) {{
                .success-container {{
                    padding: 40px 24px;
                    margin: 20px;
                }}
                
                .success-title {{
                    font-size: 24px;
                }}
                
                .action-buttons {{
                    flex-direction: column;
                    align-items: center;
                }}
                
                .btn {{
                    width: 100%;
                    max-width: 200px;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="floating-elements">
            <i class="fab fa-github"></i>
            <i class="fas fa-code"></i>
            <i class="fas fa-rocket"></i>
        </div>

        <div class="success-container">
            <div class="success-icon">
                <i class="fas fa-check"></i>
            </div>
            
            <h1 class="success-title">Project Successfully Uploaded!</h1>
            <p class="success-message">Your project has been successfully pushed to GitHub and is now live in your repository.</p>
            
            <div class="repo-card">
                <svg class="github-logo" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
                </svg>
                <div class="repo-name">
                    <i class="fas fa-folder-open"></i>
                    {repo_name}
                </div>
                <a href="{repo_url}" class="repo-url" target="_blank">
                    {repo_url.replace('https://', '')}
                    <i class="fas fa-external-link-alt external-link-icon"></i>
                </a>
            </div>
            
            <div class="action-buttons">
                <a href="{repo_url}" class="btn btn-primary" target="_blank">
                    <i class="fab fa-github"></i>
                    View Repository
                </a>
                <a href="/dashboard" class="btn btn-secondary">
                    <i class="fas fa-arrow-left"></i>
                    Back to Dashboard
                </a>
            </div>
        </div>

        <script>
            // Add a subtle entrance animation
            document.addEventListener('DOMContentLoaded', function() {{
                const container = document.querySelector('.success-container');
                container.style.opacity = '0';
                container.style.transform = 'translateY(30px)';
                
                setTimeout(() => {{
                    container.style.transition = 'all 0.6s ease';
                    container.style.opacity = '1';
                    container.style.transform = 'translateY(0)';
                }}, 100);
            }});
        </script>
    </body>
    </html>
    '''


def render_error_page(error_message):
    """Renders a professional error page with modern styling."""
    return f'''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Upload Failed - ProjectPusher</title>
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}

            body {{
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 20px;
            }}

            .error-container {{
                background: rgba(255, 255, 255, 0.95);
                backdrop-filter: blur(20px);
                border-radius: 24px;
                padding: 60px 40px;
                text-align: center;
                box-shadow: 0 25px 50px rgba(0, 0, 0, 0.15);
                max-width: 500px;
                width: 100%;
                position: relative;
                overflow: hidden;
            }}

            .error-container::before {{
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 4px;
                background: linear-gradient(90deg, #ef4444, #dc2626, #b91c1c);
            }}

            .error-icon {{
                width: 80px;
                height: 80px;
                background: linear-gradient(135deg, #ef4444, #dc2626);
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                margin: 0 auto 30px;
            }}

            .error-icon i {{
                font-size: 36px;
                color: white;
            }}

            .error-title {{
                font-size: 28px;
                font-weight: 700;
                color: #1f2937;
                margin-bottom: 16px;
                line-height: 1.2;
            }}

            .error-message {{
                font-size: 16px;
                color: #6b7280;
                margin-bottom: 40px;
                line-height: 1.6;
                padding: 20px;
                background: #fef2f2;
                border-radius: 12px;
                border-left: 4px solid #ef4444;
            }}

            .btn {{
                padding: 14px 28px;
                border-radius: 12px;
                text-decoration: none;
                font-weight: 600;
                font-size: 14px;
                transition: all 0.3s ease;
                display: inline-flex;
                align-items: center;
                gap: 8px;
                background: linear-gradient(135deg, #6366f1, #4f46e5);
                color: white;
                border: none;
            }}

            .btn:hover {{
                transform: translateY(-2px);
                box-shadow: 0 10px 25px rgba(99, 102, 241, 0.3);
            }}
        </style>
    </head>
    <body>
        <div class="error-container">
            <div class="error-icon">
                <i class="fas fa-exclamation-triangle"></i>
            </div>
            
            <h1 class="error-title">Upload Failed</h1>
            <div class="error-message">
                <strong>Error:</strong> {error_message}
            </div>
            
            <a href="/dashboard" class="btn">
                <i class="fas fa-arrow-left"></i>
                Try Again
            </a>
        </div>
    </body>
    </html>
    '''
@app.route("/logout")
def logout():
    """Logs the user out by clearing the session."""
    session.clear()
    return redirect(url_for("home"))


# --- Run the App ---
if __name__ == '__main__':
    # debug=True enables the reloader, which is useful for development.
    # The login will now work correctly even with the reloader active.
    app.run(debug=True, port=5000)