import os
import zipfile
import base64
import requests
import time
import shutil
import tempfile
import concurrent.futures
import glob
import re
import subprocess
import stat
import backoff

# Centralized API URL for maintainability
GITHUB_API_URL = "https://api.github.com"


def is_retryable_error(e):
    """
    Determines if a requests.RequestException is retryable.
    We only retry on rate-limiting (403) or server errors (5xx).
    """
    if isinstance(e, requests.exceptions.HTTPError):
        is_rate_limit = e.response.status_code == 403
        is_server_error = e.response.status_code >= 500
        return is_rate_limit or is_server_error
    return True

@backoff.on_exception(
    backoff.expo,
    requests.exceptions.RequestException,
    max_tries=5,
    giveup=lambda e: not is_retryable_error(e)
)
def _github_api_request(method, url, token, json_data=None, success_status_code=None):
    """
    A centralized helper function to make authenticated requests to the GitHub API.
    """
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        response = requests.request(method, url, headers=headers, json=json_data, timeout=30.0)
        if success_status_code and response.status_code == success_status_code:
            return {"success": True, "data": None}
        response.raise_for_status()
        if response.status_code == 204:
            return {"success": True, "data": None}
        return {"success": True, "data": response.json()}
    except requests.exceptions.RequestException as e:
        if e.response is not None:
            try:
                error_details = e.response.json().get('message', e.response.text)
                raise Exception(f"GitHub API Error: {error_details} (Status: {e.response.status_code})") from e
            except ValueError:
                raise Exception(f"GitHub API Error: {e.response.text} (Status: {e.response.status_code})") from e
        raise e


def remove_readonly(func, path, _):
    """
    Error handler for Windows read-only files during directory removal.
    This function is called by shutil.rmtree when it encounters permission errors.
    """
    try:
        # Clear the read-only bit and try again
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except (OSError, IOError):
        # If we still can't delete it, just ignore the error
        # The main operation has already succeeded
        pass


def safe_rmtree(path):
    print("Using enhanced safe_rmtree function")
    """
    Safely remove a directory tree, handling Windows permission issues.
    """
    if not os.path.exists(path):
        return
    
    try:
        # First attempt: normal removal
        shutil.rmtree(path)
    except (OSError, IOError):
        try:
            # Second attempt: with error handler for read-only files
            shutil.rmtree(path, onerror=remove_readonly)
        except (OSError, IOError):
            # Final attempt: force removal of individual files
            try:
                for root, dirs, files in os.walk(path, topdown=False):
                    for name in files:
                        file_path = os.path.join(root, name)
                        try:
                            os.chmod(file_path, stat.S_IWRITE)
                            os.remove(file_path)
                        except (OSError, IOError):
                            pass
                    for name in dirs:
                        dir_path = os.path.join(root, name)
                        try:
                            os.rmdir(dir_path)
                        except (OSError, IOError):
                            pass
                # Remove the root directory
                os.rmdir(path)
            except (OSError, IOError):
                # If all else fails, just ignore it
                # The temp directory will be cleaned up by the system eventually
                print(f"Warning: Could not fully clean up temporary directory: {path}")

def create_repo_from_zip_with_git(access_token, zip_filepath, repo_name, jobs_collection, job_id):
    """
    Creates a GitHub repository by using the local Git command line for a fast and robust upload.
    This version includes user-friendly error handling for secrets found by GitHub.
    """
    
    def update_progress(step, percentage):
        print(f"Job {job_id}: {step}")
        jobs_collection.update_one({"_id": job_id}, {"$set": {"progress": {"step": step, "percentage": percentage}}})

    IGNORE_PATTERNS = [
        'node_modules', '__pycache__', 'venv', 'env', '.DS_Store',
        'Thumbs.db', '*.pyc', 'dist', 'build', '*.log'
    ]
    tmpdir = tempfile.mkdtemp()
    
    try:
        update_progress("Preparing project...", 10)
        repo_name = re.sub(r'[\s/\\?%*:|"<>]', '-', repo_name)
        with zipfile.ZipFile(zip_filepath, 'r') as zip_ref:
            zip_ref.extractall(tmpdir)
        for pattern in IGNORE_PATTERNS:
            for path in glob.glob(os.path.join(tmpdir, '**', pattern), recursive=True):
                try:
                    if os.path.isdir(path): shutil.rmtree(path)
                    elif os.path.isfile(path): os.remove(path)
                except OSError: pass
        update_progress("Project files extracted and cleaned.", 25)

        update_progress("Initializing local Git repository...", 40)
        subprocess.run(["git", "init"], check=True, cwd=tmpdir, capture_output=True)
        subprocess.run(["git", "add", "."], check=True, cwd=tmpdir, capture_output=True)
        subprocess.run(["git", "config", "user.name", "ProjectPusher Bot"], check=True, cwd=tmpdir)
        subprocess.run(["git", "config", "user.email", "bot@example.com"], check=True, cwd=tmpdir)
        subprocess.run(["git", "commit", "-m", "feat: Initial project upload"], check=True, cwd=tmpdir, capture_output=True)

        update_progress("Creating remote repository on GitHub...", 60)
        user_response = _github_api_request("GET", f"{GITHUB_API_URL}/user", access_token)
        owner = user_response["data"]["login"]
        repo_data = {"name": repo_name, "description": "Repository created via ProjectPusher"}
        repo_creation_response = _github_api_request("POST", f"{GITHUB_API_URL}/user/repos", access_token, json_data=repo_data)
        repo_url = repo_creation_response["data"]["html_url"]
        
        update_progress("Pushing files to GitHub...", 80)
        remote_url = f"https://{access_token}@{repo_url.replace('https://', '')}.git"
        subprocess.run(["git", "branch", "-M", "main"], check=True, cwd=tmpdir, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", remote_url], check=True, cwd=tmpdir, capture_output=True)
        
        # =========================================================================
        # === THIS IS THE NEW, IMPROVED ERROR HANDLING BLOCK ===
        # =========================================================================
        try:
            subprocess.run(["git", "push", "-u", "origin", "main"], check=True, cwd=tmpdir, capture_output=True)
        except subprocess.CalledProcessError as e:
            error_output = e.stderr.decode()
            # Check if the error is the specific "secret found" violation
            if "GH013: Repository rule violations" in error_output:
                # Create a simple, user-friendly message
                user_friendly_error = (
                    "GitHub blocked this upload for your protection. "
                    "A secret (like an API key or password) was found inside your project files. "
                    "Please remove the secret, create a new .zip file, and try uploading again."
                )
                # We will still print the technical error to our own logs for debugging
                print(f"Job {job_id} failed due to secrets detected by GitHub. Full error: {error_output}")
                # We raise a new exception with the user-friendly message
                raise Exception(user_friendly_error)
            else:
                # If it's a different git error, we re-raise it to be handled by the outer block
                raise e
        # =========================================================================

        update_progress("Upload complete!", 100)
        return {"success": True, "repo_url": repo_url, "repo_name": repo_name}

    except Exception as e:
        # The user-friendly message will now be caught here
        error_message = str(e)
        print(f"An error occurred: {error_message}")
        if 'owner' in locals() and 'repo_name' in locals():
            try:
                _github_api_request("DELETE", f"{GITHUB_API_URL}/repos/{owner}/{repo_name}", access_token, success_status_code=204)
                print(f"Successfully deleted incomplete repository '{repo_name}'.")
            except Exception as cleanup_exc:
                print(f"Cleanup failed. Could not delete repository '{repo_name}'. Reason: {cleanup_exc}")
        return {"success": False, "error": error_message}
    finally:
        # The robust cleanup logic remains the same
        def on_rm_error(func, path, exc_info):
            pass
        if os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, onerror=on_rm_error)
# --- ORIGINAL API-BASED FUNCTION (kept for reference) ---
def create_repo_from_zip(access_token, zip_filepath, repo_name, jobs_collection, job_id):
    """
    Creates a GitHub repository from a ZIP file by first priming the repository
    with an initial commit, then uploading all project files via the API.
    """
    IGNORE_PATTERNS = [
        'node_modules', '__pycache__', 'venv', 'env', '.DS_Store',
        'Thumbs.db', '*.pyc', 'dist', 'build'
    ]
    DEFAULT_GITIGNORE_CONTENT = "# Standard gitignore file created by ProjectPusher\n\nnode_modules/\nvenv/\nenv/\n__pycache__/\n*.pyc\nbuild/\ndist/\n.DS_Store\n*.log\n"

    def update_progress(step, percentage):
        print(f"Job {job_id}: {step}")
        jobs_collection.update_one({"_id": job_id}, {"$set": {"progress": {"step": step, "percentage": percentage}}})

    tmpdir = tempfile.mkdtemp()
    try:
        repo_name = re.sub(r'[\s/\\?%*:|"<>]', '-', repo_name)
        update_progress("Initializing and authenticating...", 5)
        user_response = _github_api_request("GET", f"{GITHUB_API_URL}/user", access_token)
        owner = user_response["data"]["login"]
        update_progress(f"Creating repository '{repo_name}' on GitHub...", 10)
        repo_data = {"name": repo_name, "description": "Repository created via ProjectPusher", "private": False}
        repo_creation_response = _github_api_request("POST", f"{GITHUB_API_URL}/user/repos", access_token, json_data=repo_data)
        repo_url = repo_creation_response["data"]["html_url"]
        update_progress("Priming repository with initial commit...", 15)
        time.sleep(1)
        readme_content = f"# {repo_name}\n\nThis repository was created by ProjectPusher."
        readme_content_b64 = base64.b64encode(readme_content.encode('utf-8')).decode('utf-8')
        prime_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/README.md"
        prime_data = {"message": "feat: Initial commit", "content": readme_content_b64}
        prime_response = _github_api_request("PUT", prime_url, access_token, json_data=prime_data)
        parent_commit_sha = prime_response["data"]["commit"]["sha"]
        update_progress("Extracting project files...", 20)
        with zipfile.ZipFile(zip_filepath, 'r') as zip_ref:
            zip_ref.extractall(tmpdir)
        update_progress("Cleaning project directory...", 25)
        for pattern in IGNORE_PATTERNS:
            for path in glob.glob(os.path.join(tmpdir, '**', pattern), recursive=True):
                try:
                    if os.path.isdir(path): 
                        safe_rmtree(path)
                    elif os.path.isfile(path): 
                        os.chmod(path, stat.S_IWRITE)
                        os.remove(path)
                except OSError: 
                    pass
        gitignore_path = os.path.join(tmpdir, '.gitignore')
        if not os.path.exists(gitignore_path):
            with open(gitignore_path, 'w') as f: f.write(DEFAULT_GITIGNORE_CONTENT)
        def create_blob(file_path, repo_path):
            with open(file_path, "rb") as f: content = f.read()
            if not content and not repo_path.endswith('.gitkeep'): return None
            encoded_content = base64.b64encode(content).decode("utf-8")
            blob_data = {"content": encoded_content, "encoding": "base64"}
            blob_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/git/blobs"
            response = _github_api_request("POST", blob_url, access_token, json_data=blob_data)
            return {"path": repo_path, "sha": response["data"]["sha"]}
        files_to_upload = []
        for root, _, files in os.walk(tmpdir):
            for filename in files:
                file_path = os.path.join(root, filename)
                repo_path = os.path.relpath(file_path, tmpdir).replace("\\", "/")
                if repo_path.upper() == 'README.MD': continue
                files_to_upload.append((file_path, repo_path))
        if not files_to_upload:
            update_progress("No files to upload after priming. Process complete.", 100)
            return {"success": True, "repo_url": repo_url, "repo_name": repo_name}
        update_progress(f"Preparing to upload {len(files_to_upload)} project files...", 30)
        tree_items = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            future_to_blob = {executor.submit(create_blob, f[0], f[1]): f for f in files_to_upload}
            for i, future in enumerate(concurrent.futures.as_completed(future_to_blob)):
                try:
                    result = future.result()
                    if result: tree_items.append({"path": result["path"], "mode": "100644", "type": "blob", "sha": result["sha"]})
                    percentage = 30 + int((i + 1) / len(files_to_upload) * 50)
                    update_progress(f"Uploading file {i + 1} of {len(files_to_upload)}...", percentage)
                except Exception as exc:
                    raise Exception(f"Failed to create blob for a file: {exc}")
        if not tree_items:
            update_progress("Project contains only empty files. No new commit needed.", 100)
            return {"success": True, "repo_url": repo_url, "repo_name": repo_name}
        update_progress("Building repository structure...", 85)
        tree_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/git/trees"
        tree_response = _github_api_request("POST", tree_url, access_token, json_data={"tree": tree_items})
        tree_sha = tree_response["data"]["sha"]
        update_progress("Finalizing commit...", 90)
        commit_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/git/commits"
        commit_data = {"message": "feat: Upload project files", "tree": tree_sha, "parents": [parent_commit_sha]}
        commit_response = _github_api_request("POST", commit_url, access_token, json_data=commit_data)
        commit_sha = commit_response["data"]["sha"]
        update_progress("Pushing to GitHub...", 95)
        ref_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/git/refs/heads/main"
        ref_data = {"sha": commit_sha}
        _github_api_request("PATCH", ref_url, access_token, json_data=ref_data)
        update_progress("Done!", 100)
        return {"success": True, "repo_url": repo_url, "repo_name": repo_name}
    except Exception as e:
        error_message = str(e)
        print(f"An error occurred: {error_message}. Cleaning up by deleting the repository.")
        if 'owner' in locals() and 'repo_name' in locals():
            delete_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}"
            try:
                _github_api_request("DELETE", delete_url, access_token, success_status_code=204)
                print(f"Successfully deleted incomplete repository '{repo_name}'.")
            except Exception as cleanup_exc:
                print(f"Cleanup failed. Could not delete repository '{repo_name}'. Reason: {cleanup_exc}")
        return {"success": False, "error": error_message}
    finally:
        # Use the safe cleanup function
        safe_rmtree(tmpdir)


# --- ALL OTHER HELPER FUNCTIONS (UNCHANGED) ---

def get_user_repos(token):
    repos_url = f"{GITHUB_API_URL}/user/repos?sort=updated&per_page=100"
    try:
        response = _github_api_request("GET", repos_url, token)
        return response.get("data", [])
    except Exception:
        return []

def get_repo_contents(token, owner, repo_name, path=""):
    contents_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{path}"
    try:
        result = _github_api_request("GET", contents_url, token)
        contents = result['data']
        if isinstance(contents, list):
            contents.sort(key=lambda x: (x['type'] != 'dir', x['name'].lower()))
        return {"success": True, "contents": contents}
    except Exception as e:
        return {"success": False, "error": f"Could not list repository contents: {e}"}

def get_file_content(token, owner, repo_name, file_path):
    try:
        file_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{file_path}"
        response = _github_api_request("GET", file_url, token)
        file_data = response["data"]
        if 'content' not in file_data:
            return {"success": False, "error": "File content not available (it may be a directory)."}
        file_bytes = base64.b64decode(file_data['content'])
        is_binary = False
        try:
            decoded_content = file_bytes.decode('utf-8')
        except UnicodeDecodeError:
            decoded_content = ""
            is_binary = True
        return {"success": True, "content": decoded_content, "sha": file_data['sha'], "is_binary": is_binary}
    except Exception as e:
        return {"success": False, "error": f"Could not get file content: {e}"}

def update_file_in_repo(token, owner, repo_name, file_path, new_content, commit_message, sha):
    update_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{file_path}"
    encoded_content = base64.b64encode(new_content.encode('utf-8')).decode('utf-8')
    data = {"message": commit_message, "content": encoded_content, "sha": sha}
    try:
        _github_api_request("PUT", update_url, token, json_data=data)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": f"Failed to update file: {e}"}

def create_new_file(token, owner, repo_name, file_path, commit_message):
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{file_path}"
    empty_content_encoded = base64.b64encode("".encode('utf-8')).decode('utf-8')
    data = {"message": commit_message, "content": empty_content_encoded}
    try:
        _github_api_request("PUT", url, token, json_data=data)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": f"Failed to create new file: {e}"}

def create_new_folder(token, owner, repo_name, folder_path):
    placeholder_path = f"{folder_path}/.gitkeep"
    commit_message = f"feat: Create folder '{folder_path}'"
    return create_new_file(token, owner, repo_name, placeholder_path, commit_message)

def delete_repo(token, owner, repo_name):
    delete_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}"
    try:
        _github_api_request("DELETE", delete_url, token, success_status_code=204)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": f"Failed to delete repository: {e}"}

def get_repo_stats(token, owner, repo_name, stat_type="participation"):
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    stats_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/stats/{stat_type}"
    for _ in range(5):
        try:
            response = requests.get(stats_url, headers=headers)
            if response.status_code == 200:
                return {"success": True, "data": response.json()}
            elif response.status_code == 202:
                time.sleep(2)
            else:
                response.raise_for_status()
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": str(e), "data": []}
    return {"success": True, "data": []}

def get_repo_languages(token, owner, repo_name):
    languages_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/languages"
    try:
        response = _github_api_request("GET", languages_url, token)
        return {"success": True, "data": response.get("data", {})}
    except Exception as e:
        return {"success": False, "data": {}, "error": f"Failed to get languages: {e}"}

def delete_file(token, owner, repo_name, file_path, sha, commit_message):
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{file_path}"
    data = {"message": commit_message, "sha": sha}
    try:
        _github_api_request("DELETE", url, token, json_data=data, success_status_code=200)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": f"Failed to delete file: {e}"}

def create_new_file_with_content(token, owner, repo_name, file_path, content_b64, commit_message):
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/contents/{file_path}"
    data = { "message": commit_message, "content": content_b64 }
    try:
        _github_api_request("PUT", url, token, json_data=data)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": f"Failed to create file: {e}"}

def move_or_copy_item(token, owner, repo_name, source_path, destination_path, operation):
    item_name = os.path.basename(source_path)
    new_path = os.path.join(destination_path, item_name).replace("\\", "/")
    if new_path.startswith(source_path + '/') or new_path == source_path:
        return {"success": False, "error": "Cannot move an item into itself."}
    source_info_res = get_repo_contents(token, owner, repo_name, source_path)
    if isinstance(source_info_res.get("contents"), dict):
        source_file = source_info_res["contents"]
        file_content_res = get_file_content(token, owner, repo_name, source_path)
        if not file_content_res['success']: return file_content_res
        content_b64 = base64.b64encode(file_content_res['content'].encode('utf-8')).decode('utf-8')
        create_res = create_new_file_with_content(token, owner, repo_name, new_path, content_b64, f"feat: Copy '{item_name}'")
        if not create_res["success"]: return create_res
        if operation == 'cut':
            return delete_file(token, owner, repo_name, source_path, source_file["sha"], f"feat: Move '{item_name}' (delete original)")
        return {"success": True}
    elif isinstance(source_info_res.get("contents"), list):
        for item in source_info_res["contents"]:
            result = move_or_copy_item(token, owner, repo_name, item["path"], new_path, operation)
            if not result["success"]: return result
        if operation == 'cut':
            placeholder_path = f"{source_path}/.gitkeep"
            content_res = get_file_content(token, owner, repo_name, placeholder_path)
            if content_res["success"]:
                delete_file(token, owner, repo_name, placeholder_path, content_res["sha"], f"feat: Clean up folder '{source_path}'")
        return {"success": True}
    else:
        return source_info_res