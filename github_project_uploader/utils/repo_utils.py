import os
import zipfile
import base64
import requests
import time
import shutil
import tempfile
import concurrent.futures

# Centralized API URL for maintainability
GITHUB_API_URL = "https://api.github.com"

def _github_api_request(method, url, token, json_data=None, success_status_code=None):
    """
    A centralized helper function to make authenticated requests to the GitHub API.
    Handles headers, JSON data, and provides standardized error reporting.
    """
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        response = requests.request(method, url, headers=headers, json=json_data)

        # A specific success code is expected for certain actions (e.g., 204 for DELETE)
        if success_status_code and response.status_code == success_status_code:
            return {"success": True, "data": None}  # No JSON content to return on success

        # For all other requests, raise an exception for any non-2xx status code
        response.raise_for_status()

        # For GET or POST requests that return JSON content on success
        return {"success": True, "data": response.json()}

    except requests.exceptions.RequestException as e:
        error_message = f"GitHub API request failed: {e}"
        if e.response is not None:
            try:
                # Attempt to parse the specific error message from GitHub's JSON response
                error_details = e.response.json().get('message', e.response.text)
                error_message = f"GitHub API Error: {error_details} (Status: {e.response.status_code})"
            except ValueError:  # Handle cases where the response is not JSON
                error_message = f"GitHub API Error: {e.response.text} (Status: {e.response.status_code})"
        return {"success": False, "error": error_message}


def create_repo_from_zip(access_token, zip_filepath, repo_name, jobs_collection, job_id):
    """
    Creates a GitHub repository from a ZIP file using the high-performance Git Data API
    and provides real-time progress updates to a MongoDB collection.
    """

    # Helper function to report progress back to the database
    def update_progress(step, percentage):
        print(f"Job {job_id}: {step}")  # Log to the worker's console for debugging
        jobs_collection.update_one(
            {"_id": job_id},
            {"$set": {"progress": {"step": step, "percentage": percentage}}}
        )

    try:
        update_progress("Initializing and authenticating...", 5)
        user_response = _github_api_request("GET", f"{GITHUB_API_URL}/user", access_token)
        if not user_response["success"]:
            raise Exception(f"Failed to authenticate user: {user_response['error']}")
        owner = user_response["data"]["login"]

        update_progress("Creating repository on GitHub...", 10)
        repo_data = {"name": repo_name, "description": "Repository created via ProjectPusher", "private": False}
        repo_creation_response = _github_api_request("POST", f"{GITHUB_API_URL}/user/repos", access_token, json_data=repo_data)
        if not repo_creation_response["success"]:
            raise Exception(f"Failed to create repository: {repo_creation_response['error']}")
        
        repo_info = repo_creation_response["data"]
        repo_url = repo_info["html_url"]

        # Inner function to create a blob for a single file (part of the parallel process)
        def create_blob(file_path, repo_path):
            with open(file_path, "rb") as f:
                content = f.read()
            encoded_content = base64.b64encode(content).decode("utf-8")
            blob_data = {"content": encoded_content, "encoding": "base64"}
            blob_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/git/blobs"
            response = _github_api_request("POST", blob_url, access_token, json_data=blob_data)
            if not response["success"]:
                raise Exception(f"Failed to create blob for {repo_path}: {response['error']}")
            return {"path": repo_path, "sha": response["data"]["sha"]}

        tmpdir = tempfile.mkdtemp()
        update_progress("Extracting project files...", 15)
        with zipfile.ZipFile(zip_filepath, 'r') as zip_ref:
            zip_ref.extractall(tmpdir)

        files_to_upload = []
        for root, _, files in os.walk(tmpdir):
            for filename in files:
                if "__MACOSX" in root or ".DS_Store" in filename or "Thumbs.db" in filename:
                    continue
                file_path = os.path.join(root, filename)
                repo_path = os.path.relpath(file_path, tmpdir).replace("\\", "/")
                files_to_upload.append((file_path, repo_path))

        if not files_to_upload:
            raise Exception("The provided ZIP file is empty or contains no valid files to upload.")

        update_progress(f"Preparing to upload {len(files_to_upload)} files...", 20)
        tree_items = []
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_to_blob = {executor.submit(create_blob, f[0], f[1]): f for f in files_to_upload}
            for i, future in enumerate(concurrent.futures.as_completed(future_to_blob)):
                result = future.result()
                tree_items.append({"path": result["path"], "mode": "100644", "type": "blob", "sha": result["sha"]})
                # Dynamically calculate progress for the blob upload stage (from 20% to 70%)
                percentage = 20 + int((i + 1) / len(files_to_upload) * 50)
                update_progress(f"Uploading file {i + 1} of {len(files_to_upload)}...", percentage)
        
        update_progress("Building repository structure...", 80)
        tree_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/git/trees"
        tree_response = _github_api_request("POST", tree_url, access_token, json_data={"tree": tree_items})
        if not tree_response["success"]:
            raise Exception(tree_response["error"])
        tree_sha = tree_response["data"]["sha"]

        update_progress("Finalizing commit...", 90)
        commit_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/git/commits"
        commit_data = {"message": "feat: Initial project upload", "tree": tree_sha, "parents": []}
        commit_response = _github_api_request("POST", commit_url, access_token, json_data=commit_data)
        if not commit_response["success"]:
            raise Exception(commit_response["error"])
        commit_sha = commit_response["data"]["sha"]

        update_progress("Pushing to GitHub...", 95)
        ref_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/git/refs"
        ref_data = {"ref": "refs/heads/main", "sha": commit_sha}
        ref_response = _github_api_request("POST", ref_url, access_token, json_data=ref_data)
        if not ref_response["success"]:
            raise Exception(ref_response["error"])

        update_progress("Done!", 100)
        return {"success": True, "repo_url": repo_url, "repo_name": repo_name}

    except Exception as e:
        # If any step fails, delete the remote repo to avoid leaving empty repositories on the user's account.
        error_message = str(e)
        print(f"An error occurred: {error_message}. Cleaning up by deleting the repository.")
        if 'owner' in locals() and 'repo_name' in locals():
            _github_api_request("DELETE", f"{GITHUB_API_URL}/repos/{owner}/{repo_name}", access_token, success_status_code=204)
        return {"success": False, "error": error_message}
    finally:
        # Always ensure the local temporary directory is removed.
        if 'tmpdir' in locals() and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir)


def get_user_repos(token):
    """Fetches a list of the user's repositories from GitHub."""
    repos_url = f"{GITHUB_API_URL}/user/repos?sort=updated&per_page=100"
    response = _github_api_request("GET", repos_url, token)
    return response["data"] if response["success"] else []


def get_repo_contents(token, owner, repo_name, path=""):
    """Fetches the list of files and folders for the repository explorer page."""
    contents_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{path}"
    response = _github_api_request("GET", contents_url, token)
    if response["success"]:
        contents = response["data"]
        # Sort contents to show folders first, then files, all alphabetically.
        contents.sort(key=lambda x: (x['type'] != 'dir', x['name'].lower()))
        return {"success": True, "contents": contents}
    return response


def get_file_content(token, owner, repo_name, file_path):
    """Fetches the content of a single file for the editor page."""
    file_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{file_path}"
    response = _github_api_request("GET", file_url, token)
    if not response["success"]:
        return response

    file_data = response["data"]
    if 'content' not in file_data:
        return {"success": False, "error": "File content not available (it may be a directory)."}
    
    file_bytes = base64.b64decode(file_data['content'])
    is_binary = False
    try:
        decoded_content = file_bytes.decode('utf-8')
    except UnicodeDecodeError:
        decoded_content = ""  # Don't send binary content to the web editor
        is_binary = True
        
    return {"success": True, "content": decoded_content, "sha": file_data['sha'], "is_binary": is_binary}


def update_file_in_repo(token, owner, repo_name, file_path, new_content, commit_message, sha):
    """Saves and commits a change to a single file."""
    update_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{file_path}"
    encoded_content = base64.b64encode(new_content.encode('utf-8')).decode('utf-8')
    data = {"message": commit_message, "content": encoded_content, "sha": sha}
    return _github_api_request("PUT", update_url, token, json_data=data)


def create_new_file(token, owner, repo_name, file_path, commit_message):
    """Creates a new, empty file in the repository."""
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{file_path}"
    empty_content_encoded = base64.b64encode("".encode('utf-8')).decode('utf-8')
    data = {"message": commit_message, "content": empty_content_encoded}
    return _github_api_request("PUT", url, token, json_data=data)


def create_new_folder(token, owner, repo_name, folder_path):
    """Creates a new folder by creating a placeholder .gitkeep file inside it."""
    placeholder_path = f"{folder_path}/.gitkeep"
    commit_message = f"feat: Create folder '{folder_path}'"
    return create_new_file(token, owner, repo_name, placeholder_path, commit_message)


def delete_repo(token, owner, repo_name):
    """Permanently deletes a repository from the user's GitHub account."""
    delete_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}"
    # A successful DELETE request returns a 204 No Content status.
    return _github_api_request("DELETE", delete_url, token, success_status_code=204)


def get_repo_stats(token, owner, repo_name, stat_type="participation"):
    """
    Fetches specific statistics, handling GitHub's 202 'processing' response.
    Note: This function retains custom logic due to its unique 202 status handling.
    """
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    stats_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/stats/{stat_type}"
    
    # Try up to 5 times to get the stats
    for _ in range(5):
        try:
            response = requests.get(stats_url, headers=headers)
            if response.status_code == 200:
                return {"success": True, "data": response.json()}
            elif response.status_code == 202:
                time.sleep(2)  # Wait for GitHub to generate the stats
            else:
                response.raise_for_status()
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": str(e), "data": []}
            
    # If it's still 202 after several tries, return empty data to avoid a long wait.
    return {"success": True, "data": []}


def get_repo_languages(token, owner, repo_name):
    """Fetches the language breakdown for a single repository."""
    languages_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/languages"
    response = _github_api_request("GET", languages_url, token)
    return {"success": response["success"], "data": response.get("data", {}), "error": response.get("error")}