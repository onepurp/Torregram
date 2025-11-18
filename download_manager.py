
import asyncio
import os
import shutil
import libtorrent as lt
from telegram.ext import Application, ContextTypes

import config
from state import AppState
# --- FIX: Import the correct, renamed function ---
from telegram_uploader import refresh_status_panel

async def start_download_job(app: Application, app_state: AppState, session, item: dict):
    info_hash_str = item["info_hash"]
    torrent_data = app_state.active_torrents.get(info_hash_str)
    if not torrent_data: return

    torrent_data['user_chat_id'] = item["chat_id"]
    if not torrent_data.get('status_message_id'):
        info = torrent_data["handle"].torrent_file()
        # --- FIX: Use the new function to create the initial panel ---
        status_message = await app.bot.send_message(chat_id=item["chat_id"], text=f"⏳ Queued `{info.name()}`...")
        torrent_data['status_message_id'] = status_message.message_id
        await refresh_status_panel(app.bot, app_state, info_hash_str, "Waiting for available space...")


    handle = torrent_data["handle"]
    files = handle.torrent_file().files()
    priorities = [1 if i in torrent_data["files_to_download"].keys() else 0 for i in range(files.num_files())]
    handle.prioritize_files(priorities)
    handle.resume()

    job_name = f"job_{info_hash_str}"
    if not app.job_queue.get_jobs_by_name(job_name):
        job_data = {"info_hash": info_hash_str, "app_state": app_state, "session": session}
        app.job_queue.run_repeating(monitor_download, interval=10, first=0, data=job_data, name=job_name)

async def monitor_download(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    info_hash_str = job_data["info_hash"]
    app_state: AppState = job_data["app_state"]
    session = job_data["session"]
    
    torrent_data = app_state.active_torrents.get(info_hash_str)
    if not torrent_data:
        context.job.schedule_removal()
        return

    handle = torrent_data["handle"]
    if not handle.is_valid():
        context.job.schedule_removal()
        return

    status = handle.status()
    info = handle.torrent_file()
    if not info: return

    if status.state == lt.torrent_status.error:
        error_msg = status.error_message() if status.error_message() else "Unknown error"
        print(f"CRITICAL ERROR for '{info.name()}': {error_msg}. Stopping job.")
        await refresh_status_panel(context.bot, app_state, info_hash_str, f"❌ Download Failed: {error_msg}", is_final=True)
        session.remove_torrent(handle, lt.session.delete_files)
        
        if info_hash_str in app_state.torrent_metadata_cache:
            temp_torrent_path = app_state.torrent_metadata_cache.pop(info_hash_str)
            if os.path.exists(temp_torrent_path): os.remove(temp_torrent_path)
        
        del app_state.active_torrents[info_hash_str]
        context.job.schedule_removal()
        return

    # --- FIX: Call the correct, renamed function ---
    await refresh_status_panel(context.bot, app_state, info_hash_str, "Downloading...")

    if status.state in (lt.torrent_status.seeding, lt.torrent_status.finished):
        files = info.files()
        for i in list(torrent_data["files_to_download"].keys()):
            full_path = os.path.join("./downloads", files.file_path(i))
            
            if os.path.exists(full_path) and full_path not in torrent_data["download_complete_files"]:
                print(f"File '{os.path.basename(full_path)}' confirmed stable. Adding to upload queue.")
                
                file_options = torrent_data["files_to_download"][i]
                should_extract = file_options.get("extract", False)
                
                await app_state.upload_queue.put({
                    "path": full_path,
                    "info_hash": info_hash_str,
                    "extract": should_extract
                })
                
                torrent_data["download_complete_files"].append(full_path)
        
        if not torrent_data.get("seeding_paused"):
            print(f"Download complete for '{info.name()}'. Pausing torrent to stop seeding.")
            handle.pause()
            torrent_data["seeding_paused"] = True

async def download_manager_worker(app: Application, app_state: AppState, session):
    print("Download manager worker started.")
    while True:
        await app_state.new_download_event.wait()

        if app_state.download_queue.empty():
            app_state.new_download_event.clear()
            continue

        try:
            total, used, free = await asyncio.to_thread(shutil.disk_usage, '.')
            committed_space = 0
            for data in app_state.active_torrents.values():
                if data["handle"].is_valid():
                    s = data["handle"].status()
                    if not s.state == lt.torrent_status.seeding:
                        committed_space += s.total_wanted - s.total_wanted_done
            
            buffer = config.STORAGE_BUFFER_GB * (1024**3)
            effective_available_space = free - committed_space - buffer
            
            next_item = app_state.download_queue._queue[0]
            
            if next_item["total_size"] <= effective_available_space:
                print(f"Sufficient space for download {next_item['info_hash']}. Starting...")
                job_to_start = await app_state.download_queue.get()
                await start_download_job(app, app_state, session, job_to_start)
            else:
                print(f"Insufficient space for download {next_item['info_hash']}. Waiting for space to free up.")
                app_state.new_download_event.clear()
        
        except Exception as e:
            print(f"Error in download_manager_worker: {e}")
            await asyncio.sleep(30)