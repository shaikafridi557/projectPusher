import os
import zipfile
import tempfile
import requests
import git
import shutil
import stat
import base64
import time # <-- NEW IMPORT

GITHUB_API_URL = "https://api.github.com"

def _handle_rmtree_error(func, path, exc_info):
    """A robust error handler to fix file permission errors on Windows."""
    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR)
        func(path)
    else:
        raise

def create_repo_from_zip(access_token, zip_file_storage, repo_name):
    """Creates a perfect, hosting-ready GitHub repository with the correct file structure."""
    headers = {"Authorization": f"token {access_token}", "Accept": "application/vnd.github.v3+json"}
    repo_data = { "name": repo_name, "description": "Repository created with ProjectPusher" }
    try:
        repo_creation_response = requests.post(f"{GITHUB_API_URL}/user/repos", headers=headers, json=repo_data)
        repo_creation_response.raise_for_status()
    except requests.exceptions.RequestException as e:
        error_message = e.response.json().get('message', 'Unknown error')
        return {"success": False, "error": f"Failed to create repository: {error_message}"}
        
    repo_info = repo_creation_response.json()
    owner = repo_info["owner"]["login"]
    repo_url = repo_info["html_url"]
    authenticated_clone_url = f"https://{owner}:{access_token}@{repo_url.replace('https://', '')}"
    
    tmpdir = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(zip_file_storage, 'r') as zip_ref:
            zip_ref.extractall(tmpdir)
            
        project_root = tmpdir
        dir_contents = os.listdir(tmpdir)
        # Handle cases where the zip file contains a single root folder
        if len(dir_contents) == 1 and os.path.isdir(os.path.join(tmpdir, dir_contents[0])):
            project_root = os.path.join(tmpdir, dir_contents[0])
            
        repo = git.Repo.init(project_root)
        repo.git.add(A=True)
        repo.index.commit("Initial commit from ProjectPusher")
        
        # Ensure the default branch is named 'main'
        if 'master' in repo.heads:
            repo.heads.master.rename('main')
            
        origin = repo.create_remote('origin', authenticated_clone_url)
        origin.push(refspec='HEAD:main')
        repo.close()
    except Exception as e:
        # If git operations fail, delete the created GitHub repo to avoid orphans
        requests.delete(f"{GITHUB_API_URL}/repos/{owner}/{repo_name}", headers=headers)
        return {"success": False, "error": f"A technical error occurred during the Git push: {e}"}
    finally:
        shutil.rmtree(tmpdir, onerror=_handle_rmtree_error)
        
    return {"success": True, "repo_url": repo_url, "repo_name": repo_name}

def get_user_repos(token):
    """Fetches a list of the user's repositories from GitHub, sorted by last update."""
    headers = { "Authorization": f"token {token}", "Accept": "application/vnd.github+json" }
    # Fetch up to 100 repos to get a good overview
    repos_url = f"{GITHUB_API_URL}/user/repos?sort=updated&per_page=100"
    
    try:
        response = requests.get(repos_url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException:
        # Return an empty list on failure so the app doesn't crash
        return []

def get_repo_contents(token, owner, repo_name, path=""):
    """Fetches the list of files and folders for the repository explorer page."""
    headers = { "Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json" }
    contents_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{path}"
    try:
        response = requests.get(contents_url, headers=headers)
        response.raise_for_status()
        contents = response.json()
        # Sort to show folders first, then files, all alphabetically
        contents.sort(key=lambda x: (x['type'] != 'dir', x['name'].lower()))
        return {"success": True, "contents": contents}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"Could not list repository contents: {e}"}

def get_file_content(token, owner, repo_name, file_path):
    """Fetches the content of a single file for the editor page."""
    headers = { "Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json" }
    file_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{file_path}"
    try:
        response = requests.get(file_url, headers=headers)
        response.raise_for_status()
        file_data = response.json()
        
        if 'content' not in file_data:
            return {"success": False, "error": "File content not available (might be a submodule or too large)."}
            
        file_bytes = base64.b64decode(file_data['content'])
        try:
            # Try to decode as UTF-8
            decoded_content = file_bytes.decode('utf-8')
            is_binary = False
        except UnicodeDecodeError:
            # If it fails, treat the file as binary
            decoded_content = ""
            is_binary = True
            
        file_sha = file_data['sha']
        return {"success": True, "content": decoded_content, "sha": file_sha, "is_binary": is_binary}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"Could not get file content: {e}"}

def update_file_in_repo(token, owner, repo_name, file_path, new_content, commit_message, sha):
    """Saves and commits a change to a single file."""
    headers = { "Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json" }
    update_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{file_path}"
    encoded_new_content = base64.b64encode(new_content.encode('utf-8')).decode('utf-8')
    data = {"message": commit_message, "content": encoded_new_content, "sha": sha}
    try:
        response = requests.put(update_url, headers=headers, json=data)
        response.raise_for_status()
        return {"success": True}
    except requests.exceptions.RequestException as e:
        error_message = e.response.json().get('message', 'Unknown error')
        return {"success": False, "error": f"Failed to save file: {error_message}"}

def create_new_file(token, owner, repo_name, file_path, commit_message):
    """Creates a new, empty file in the repository."""
    headers = { "Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json" }
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{file_path}"
    empty_content_encoded = base64.b64encode("".encode('utf-8')).decode('utf-8')
    data = { "message": commit_message, "content": empty_content_encoded }
    try:
        response = requests.put(url, headers=headers, json=data)
        response.raise_for_status()
        return {"success": True}
    except requests.exceptions.RequestException as e:
        error_message = e.response.json().get('message', 'Unknown error')
        return {"success": False, "error": f"Failed to create file: {error_message}"}

def create_new_folder(token, owner, repo_name, folder_path):
    """Creates a new folder by creating a placeholder .gitkeep file inside it."""
    # GitHub creates folders implicitly when a file is added to a non-existent path.
    placeholder_path = f"{folder_path}/.gitkeep"
    commit_message = f"feat: Create folder '{folder_path}'"
    return create_new_file(token, owner, repo_name, placeholder_path, commit_message)

def delete_repo(token, owner, repo_name):
    """Permanently deletes a repository from the user's GitHub account."""
    headers = { "Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json" }
    delete_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}"
    try:
        response = requests.delete(delete_url, headers=headers)
        if response.status_code == 204:
            return {"success": True}
        else:
            error_message = response.json().get('message', 'Unknown error during deletion.')
            return {"success": False, "error": error_message}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"A network error occurred: {e}"}

# --- NEW FUNCTIONS FOR DYNAMIC ANALYTICS ---

def get_repo_stats(token, owner, repo_name, stat_type="participation"):
    """
    Fetches specific statistics for a repository (e.g., 'participation', 'commit_activity').
    Handles GitHub's 202 response for cached data by retrying.
    """
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    stats_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/stats/{stat_type}"
    
    # Retry logic for when GitHub is calculating stats
    for _ in range(5):  # Try up to 5 times
        try:
            response = requests.get(stats_url, headers=headers)
            if response.status_code == 200:
                return {"success": True, "data": response.json()}
            elif response.status_code == 202:
                # GitHub is still calculating, wait for 2 seconds and retry
                time.sleep(2)
            else:
                # For other errors, raise an exception to be caught below
                response.raise_for_status()
        except requests.exceptions.RequestException as e:
            # For a single repo failure, we can return empty data and a non-fatal error
            return {"success": False, "error": str(e), "data": []}
            
    # If it's still 202 after several tries, return empty but successful to not break the loop
    return {"success": True, "data": []}

def get_repo_languages(token, owner, repo_name):
    """Fetches the language breakdown for a single repository."""
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    languages_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/languages"
    try:
        response = requests.get(languages_url, headers=headers)
        response.raise_for_status()
        return {"success": True, "data": response.json()}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": str(e), "data": {}}