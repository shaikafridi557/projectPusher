# background_worker.py (Updated to handle 'is_private' feature)

import time
import os
import stat
import shutil
from pymongo import MongoClient
from dotenv import load_dotenv

# Load environment variables. This is important for the thread too.
load_dotenv()

def safe_file_remove(filepath):
    """
    Safely remove a file, handling Windows permission issues.
    """
    if not os.path.exists(filepath):
        return True
    
    try:
        # First attempt: normal removal
        os.remove(filepath)
        return True
    except (OSError, IOError):
        try:
            # Second attempt: clear read-only and try again
            os.chmod(filepath, stat.S_IWRITE)
            os.remove(filepath)
            return True
        except (OSError, IOError):
            # If we still can't delete it, just log and continue
            print(f"Warning: Could not remove temporary file: {filepath}")
            return False

def get_mongo_collection():
    """Connects to MongoDB and returns the 'jobs' collection."""
    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        print("WORKER ERROR: MONGO_URI is not set.")
        raise ValueError("FATAL: MONGO_URI is not set in the environment.")
        
    print("Connecting to MongoDB for worker...")
    client = MongoClient(mongo_uri)
    
    try:
        client.admin.command('ping')
        print("MongoDB connection for worker successful.")
    except Exception as e:
        print(f"WORKER ERROR: Could not connect to MongoDB. Error: {e}")
        raise
        
    db = client.get_default_database()
    return db.jobs

def process_jobs():
    """
    This function runs in an infinite loop, continuously checking MongoDB for new, queued jobs.
    It's designed to be run in a background thread.
    """
    try:
        jobs_collection = get_mongo_collection()
        print("Background worker thread started. Looking for new jobs...")
    except Exception as e:
        print(f"Background worker could not start due to a setup error: {e}")
        return # Exit the thread if DB connection fails

    from utils.repo_utils import create_repo_from_zip_with_git

    while True:
        job = None
        try:
            # Atomically find a 'queued' job and update its status to 'processing'.
            job = jobs_collection.find_one_and_update(
                {"status": "queued"},
                {
                    "$set": {
                        "status": "processing",
                        "progress": {"step": "Preparing to process...", "percentage": 0}
                    }
                }
            )

            if job:
                print(f"Worker found job {job['_id']}. Processing with Git command-line method...")

                try:
                    # ===================================================================
                    # === THIS IS THE MODIFIED FUNCTION CALL ===
                    # ===================================================================
                    
                    # 1. Get the 'is_private' flag from the job document.
                    #    Default to False if it's not present for any reason.
                    is_private_job = job.get('is_private', False)
                    
                    # 2. Call the updated function with the new 'is_private_job' argument.
                    result = create_repo_from_zip_with_git(
                        job["access_token"],
                        job["temp_filepath"],
                        job["repo_name"],
                        is_private_job, # <-- The new argument is passed here
                        jobs_collection,
                        job["_id"]
                    )
                    
                    # ===================================================================

                    final_status = 'finished' if result.get('success') else 'failed'
                    jobs_collection.update_one(
                        {"_id": job["_id"]},
                        {"$set": {"status": final_status, "result": result}}
                    )
                    print(f"Job {job['_id']} finished with status: {final_status}")

                except Exception as repo_error:
                    # Handle errors from the repo creation function
                    print(f"Error during repository creation for job {job['_id']}: {repo_error}")
                    error_result = {"success": False, "error": f"Repository creation failed: {str(repo_error)}"}
                    jobs_collection.update_one(
                        {"_id": job["_id"]},
                        {"$set": {"status": "failed", "result": error_result}}
                    )

            else:
                # If no job, wait for 5 seconds before checking again.
                time.sleep(5)

        except Exception as e:
            print(f"An unexpected error occurred in the worker loop: {e}")
            if job:
                error_result = {"success": False, "error": f"A fatal worker process error occurred: {str(e)}"}
                try:
                    jobs_collection.update_one(
                        {"_id": job["_id"]},
                        {"$set": {"status": "failed", "result": error_result}}
                    )
                except Exception as db_error:
                    print(f"Could not update job status in database: {db_error}")
            time.sleep(10)

        finally:
            # Enhanced cleanup with better error handling
            if job and job.get("temp_filepath"):
                try:
                    if os.path.exists(job["temp_filepath"]):
                        success = safe_file_remove(job["temp_filepath"])
                        if success:
                            print(f"Cleaned up temporary file: {job['temp_filepath']}")
                        else:
                            print(f"Partial cleanup - could not remove: {job['temp_filepath']}")
                except Exception as e:
                    print(f"Error during file cleanup for job {job.get('_id', 'unknown')}: {e}")