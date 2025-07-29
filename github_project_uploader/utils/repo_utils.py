# In utils/repo_utils.py

import os
import zipfile
import tempfile
import requests
import git      # The GitPython library
import shutil   # For robust cleanup
import stat     # For handling permissions during cleanup

GITHUB_API_URL = "https://api.github.com"

def _handle_rmtree_error(func, path, exc_info):
    """A robust error handler to fix file permission errors on Windows."""
    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR)
        func(path)
    else:
        raise

def create_repo_from_zip(access_token, zip_file_storage, repo_name):
    """
    Creates a perfect, hosting-ready GitHub repository with the correct file structure.
    """
    headers = {
        "Authorization": f"token {access_token}",
        "Accept": "application/vnd.github.v3+json"
    }

    # Step 1: Create a new, EMPTY repository on GitHub.
    repo_data = { "name": repo_name, "description": "Repository created with ProjectPusher" }
    
    try:
        repo_creation_response = requests.post(f"{GITHUB_API_URL}/user/repos", headers=headers, json=repo_data)
        repo_creation_response.raise_for_status()
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"Failed to create repository: {e.response.json().get('message')}"}

    repo_info = repo_creation_response.json()
    owner = repo_info["owner"]["login"]
    repo_url = repo_info["html_url"]
    authenticated_clone_url = f"https://{owner}:{access_token}@{repo_url.replace('https://', '')}"

    tmpdir = tempfile.mkdtemp()
    
    try:
        # Step 2: Unzip the project files into the temp folder.
        with zipfile.ZipFile(zip_file_storage, 'r') as zip_ref:
            zip_ref.extractall(tmpdir)

        # --- THIS IS THE NEW, SMART LOGIC ---
        # We figure out the real root of the project inside the zip.
        project_root = tmpdir
        dir_contents = os.listdir(tmpdir)
        # If the zip file contains a single folder at its root,
        # we'll use that folder as the starting point for our git commands.
        if len(dir_contents) == 1 and os.path.isdir(os.path.join(tmpdir, dir_contents[0])):
            project_root = os.path.join(tmpdir, dir_contents[0])
            print(f"Detected nested project folder. Using '{project_root}' as the root.")
        # --- END OF NEW LOGIC ---

        # Step 3: Run the Git commands from the CORRECT project root.
        # This fixes the "box inside a box" problem.
        repo = git.Repo.init(project_root)
        repo.git.add(A=True)
        repo.index.commit("Initial commit from ProjectPusher")
        origin = repo.create_remote('origin', authenticated_clone_url)
        origin.push(refspec='HEAD:main')
        
        # Explicitly close the repo to release file locks (fixes Windows errors)
        repo.close()
        
    except Exception as e:
        # If anything fails, clean up the empty repo on GitHub.
        requests.delete(f"{GITHUB_API_URL}/repos/{owner}/{repo_name}", headers=headers)
        return {"success": False, "error": f"A technical error occurred during the Git push: {e}"}
    
    finally:
        # Always clean up the temporary folder.
        shutil.rmtree(tmpdir, onerror=_handle_rmtree_error)

    return {"success": True, "repo_url": repo_url, "repo_name": repo_name}


def get_user_repos(token):
    """Fetches a list of the user's repositories from GitHub."""
    headers = { "Authorization": f"token {token}", "Accept": "application/vnd.github+json" }
    try:
        response = requests.get(f"{GITHUB_API_URL}/user/repos?sort=updated&per_page=20", headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException:
        return []