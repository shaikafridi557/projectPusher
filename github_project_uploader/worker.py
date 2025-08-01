import time
import os
from pymongo import MongoClient
from dotenv import load_dotenv

# Load environment variables from .env file.
# This is a critical step for accessing the database URI.
load_dotenv()

def get_mongo_collection():
    """Connects to MongoDB and returns the 'jobs' collection."""
    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        # This is a common setup error, so the message is made very clear.
        raise ValueError("FATAL: MONGO_URI is not set in the environment. Please check your .env file.")
        
    print("Connecting to MongoDB...")
    client = MongoClient(mongo_uri)
    
    # Test the connection to ensure the MongoDB server is running and accessible.
    try:
        client.admin.command('ping')
        print("MongoDB connection successful.")
    except Exception as e:
        print(f"FATAL: Could not connect to MongoDB. Is the server running? Error: {e}")
        raise  # Stop the script if a database connection cannot be established.
        
    db = client.get_default_database()
    return db.jobs

def process_jobs():
    """
    This function runs in an infinite loop, continuously checking MongoDB for new, queued jobs.
    """
    try:
        jobs_collection = get_mongo_collection()
        print("Worker started. Looking for new jobs in MongoDB...")
    except Exception as e:
        # If the worker cannot even get a connection to the collection, it cannot start.
        print(f"Worker could not start due to a setup error: {e}")
        return  # Exit the function and stop the script.

    # We only import the function here, inside the function that uses it.
    # This ensures that any changes to repo_utils are picked up if the worker restarts.
    from utils.repo_utils import create_repo_from_zip

    while True:
        job = None
        try:
            # Atomically find a 'queued' job and update its status to 'processing'.
            # This prevents multiple workers from picking up the same job.
            job = jobs_collection.find_one_and_update(
                {"status": "queued"},
                {
                    "$set": {
                        "status": "processing",
                        # Set the initial progress state for the UI.
                        "progress": {"step": "Preparing to process...", "percentage": 0}
                    }
                }
            )

            if job:
                print(f"Found job {job['_id']}. Processing...")

                # --- UPDATED FUNCTION CALL ---
                # Pass the database collection and job ID to allow for real-time progress updates.
                result = create_repo_from_zip(
                    job["access_token"],
                    job["temp_filepath"],
                    job["repo_name"],
                    jobs_collection,  # Argument 1: The collection object.
                    job["_id"]          # Argument 2: The specific job's ID.
                )

                # Determine the final status based on the result from the processing function.
                final_status = 'finished' if result.get('success') else 'failed'
                jobs_collection.update_one(
                    {"_id": job["_id"]},
                    {"$set": {"status": final_status, "result": result}}
                )

                print(f"Job {job['_id']} finished with status: {final_status}")
            
            else:
                # If no job was found, wait for 5 seconds before checking again.
                time.sleep(5)

        except Exception as e:
            # This is a catch-all for any unexpected errors during the job processing loop.
            print(f"An unexpected error occurred in the worker loop: {e}")
            if job:
                # If a job was being processed when the error occurred, mark it as 'failed'.
                error_result = {"success": False, "error": f"A fatal worker process error occurred: {str(e)}"}
                jobs_collection.update_one(
                    {"_id": job["_id"]},
                    {"$set": {"status": "failed", "result": error_result}}
                )
            # Wait longer after an error to prevent rapid-fire failure loops.
            time.sleep(10)

        finally:
            # This block ensures that the temporary zip file is always deleted,
            # regardless of whether the job succeeded or failed.
            if job and job.get("temp_filepath"):
                try:
                    if os.path.exists(job["temp_filepath"]):
                        os.remove(job["temp_filepath"])
                        print(f"Cleaned up temporary file: {job['temp_filepath']}")
                except Exception as e:
                    # Log an error if cleanup fails, but don't stop the worker.
                    print(f"Error during file cleanup for job {job['_id']}: {e}")

if __name__ == '__main__':
    process_jobs()