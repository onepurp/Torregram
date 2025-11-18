# telegram_uploader.py
import asyncio
import json
import os
import subprocess
import time
import aiohttp
import shlex
import zipfile
import rarfile
import py7zr
import shutil
import uuid
import libtorrent as lt
import math

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeAudio
from telegram import Bot
from telegram.error import BadRequest

import config
from state import AppState

INDEX_FILE = "channel_index.json"

# ... (All functions from the top down to upload_with_telethon are unchanged) ...
def load_index_from_disk(app_state: AppState):
    print("Loading channel file index from disk...")
    try:
        if os.path.exists(INDEX_FILE):
            with open(INDEX_FILE, 'r') as f:
                data = json.load(f)
                app_state.channel_file_index = {tuple(item) for item in data}
                print(f"Loaded {len(app_state.channel_file_index)} file fingerprints from {INDEX_FILE}.")
        else:
            print("Index file not found. A new one will be created.")
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error loading index file: {e}. Starting with an empty index.")
        app_state.channel_file_index = set()

async def save_fingerprint_to_disk(filename: str, filesize: int):
    try:
        data = []
        if os.path.exists(INDEX_FILE) and os.path.getsize(INDEX_FILE) > 0:
            with open(INDEX_FILE, 'r') as f:
                data = json.load(f)
        
        data.append([filename, filesize])
        
        with open(INDEX_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except (IOError, json.JSONDecodeError) as e:
        print(f"CRITICAL: Could not save new fingerprint to index file: {e}")

STATE_MAP = {
    lt.torrent_status.states.queued_for_checking: "Queued",
    lt.torrent_status.states.checking_files: "Checking",
    lt.torrent_status.states.downloading_metadata: "Fetching Metadata",
    lt.torrent_status.states.downloading: "Downloading",
    lt.torrent_status.states.finished: "Finished",
    lt.torrent_status.states.seeding: "Seeding",
    lt.torrent_status.states.allocating: "Allocating",
    lt.torrent_status.states.checking_resume_data: "Resuming",
}

def format_bytes(size_bytes):
    if size_bytes == 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def format_time(seconds):
    if seconds is None or seconds == float('inf') or seconds < 0:
        return "∞"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d > 0: parts.append(f"{int(d)}d")
    if h > 0: parts.append(f"{int(h)}h")
    if m > 0: parts.append(f"{int(m)}m")
    if s > 0 or not parts: parts.append(f"{int(s)}s")
    return " ".join(parts)

def create_progress_bar(progress, length=10):
    filled_length = int(length * progress)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return f"[{bar}]"

async def update_status_message(bot: Bot, app_state: AppState, info_hash_str: str, current_task: str, is_final: bool = False):
    torrent_data = app_state.active_torrents.get(info_hash_str)
    if not torrent_data or not torrent_data.get('user_chat_id') or not torrent_data.get('status_message_id'):
        return

    handle = torrent_data["handle"]
    if not handle.is_valid(): return
    
    status = handle.status()
    info = handle.torrent_file()
    if not info: return

    if is_final:
        message = f"**Torrent:** `{info.name()}`\n\n"
        message += f"**Final Report:**\n{current_task}"
    else:
        progress_percent = status.progress
        progress_bar = create_progress_bar(progress_percent)
        
        state_str = STATE_MAP.get(status.state, 'N/A')
        download_speed = format_bytes(status.download_rate) + '/s'
        upload_speed = format_bytes(status.upload_rate) + '/s'
        
        total_wanted = status.total_wanted
        total_downloaded = status.total_download
        
        eta_seconds = (total_wanted - total_downloaded) / status.download_rate if status.download_rate > 0 else float('inf')
        eta_str = format_time(eta_seconds)
        
        jobs_done = torrent_data['jobs_completed']
        jobs_total = torrent_data['jobs_total']

        message = (
            f"**Torrent:** `{info.name()}`\n\n"
            f"`{progress_bar} {progress_percent * 100:.1f}%`\n\n"
            f"**📊 Stats**\n"
            f"` D: {download_speed} | U: {upload_speed} `\n"
            f"` Total: {format_bytes(total_wanted)} | State: {state_str} `\n"
            f"` Peers: {status.num_peers} `\n\n"
            f"**📈 Progress**\n"
            f"` Jobs: {jobs_done} / {jobs_total} `\n"
            f"` ETA: {eta_str} `\n\n"
            f"**⚡ Current Task:**\n"
            f"`{current_task}`"
        )

    try:
        await bot.edit_message_text(chat_id=torrent_data['user_chat_id'], message_id=torrent_data['status_message_id'], text=message, parse_mode="Markdown")
    except (BadRequest, Exception) as e:
        if "Message is not modified" not in str(e):
            print(f"Error updating status panel (ignoring): {e}")

def _extract_sync(archive_path, extract_dir):
    try:
        if archive_path.lower().endswith('.zip'):
            with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
        elif archive_path.lower().endswith('.rar'):
            with rarfile.RarFile(archive_path, 'r') as rar_ref:
                rar_ref.extractall(extract_dir)
        elif archive_path.lower().endswith('.7z'):
            with py7zr.SevenZipFile(archive_path, mode='r') as z_ref:
                z_ref.extractall(path=extract_dir)
        return True
    except Exception as e:
        print(f"Extraction failed for {os.path.basename(archive_path)}: {e}")
        return False

async def process_archive(app, app_state: AppState, archive_path: str, info_hash_str: str):
    torrent_data = app_state.active_torrents.get(info_hash_str)
    if not torrent_data: return

    extract_dir = os.path.join("temp", str(uuid.uuid4()))
    
    try:
        os.makedirs(extract_dir, exist_ok=True)
        success = await asyncio.to_thread(_extract_sync, archive_path, extract_dir)
        
        if success:
            extracted_items = []
            for root, _, files in os.walk(extract_dir):
                for file in files:
                    extracted_items.append(os.path.join(root, file))
            
            if extracted_items:
                async with app_state.torrent_locks[info_hash_str]:
                    torrent_data["jobs_total"] += len(extracted_items)
                
                for file_path in extracted_items:
                    is_nested_archive = file_path.lower().endswith(config.ARCHIVE_EXTENSIONS)
                    new_item = {
                        "path": file_path, "info_hash": info_hash_str,
                        "extract": is_nested_archive, "is_extracted_content": True
                    }
                    await app_state.upload_queue.put(new_item)
            else:
                await update_status_message(app.bot, app_state, info_hash_str, f"ℹ️ Archive `{os.path.basename(archive_path)}` was empty.")

    finally:
        if os.path.exists(archive_path):
            os.remove(archive_path)

async def get_media_metadata(file_path: str) -> dict | None:
    try:
        safe_file_path = shlex.quote(file_path)
        command = f"ffprobe -v error -show_format -show_streams -of json {safe_file_path}"
        proc = await asyncio.to_thread(
            subprocess.run, command, shell=True, check=True, capture_output=True, text=True
        )
        data = json.loads(proc.stdout)
        metadata = {}
        if 'streams' in data and data['streams']:
            video_stream = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
            audio_stream = next((s for s in data['streams'] if s['codec_type'] == 'audio'), None)
            if video_stream:
                metadata['width'] = video_stream.get('width', 0)
                metadata['height'] = video_stream.get('height', 0)
                metadata['duration'] = int(float(video_stream.get('duration', 0)))
            if audio_stream and not metadata.get('duration'):
                 metadata['duration'] = int(float(audio_stream.get('duration', 0)))
        if 'format' in data and not metadata.get('duration'):
            metadata['duration'] = int(float(data['format'].get('duration', 0)))
        if 'format' in data and 'tags' in data['format']:
            tags = data['format']['tags']
            metadata['title'] = tags.get('title')
            metadata['artist'] = tags.get('artist')
        return metadata
    except Exception as e:
        print(f"Could not get media metadata for {os.path.basename(file_path)}: {e}")
        return None

async def prepare_file_for_upload(file_path: str) -> str | None:
    if not os.path.exists(file_path):
        await asyncio.sleep(2)
        if not os.path.exists(file_path):
            print(f"Preparation failed: File not found at {file_path}")
            return None
    _, extension = os.path.splitext(file_path)
    extension = extension.lower()
    if extension not in config.VIDEO_EXTENSIONS:
        return file_path
    
    print(f"Preparing video for streaming: {os.path.basename(file_path)}")
    output_dir = os.path.dirname(file_path)
    base_name, _ = os.path.splitext(os.path.basename(file_path))
    output_path = os.path.join(output_dir, f"{base_name}_streamable.mp4")
    
    safe_input_path = shlex.quote(file_path)
    safe_output_path = shlex.quote(output_path)
    command = f"ffmpeg -i {safe_input_path} -c copy -movflags +faststart {safe_output_path}"
    try:
        await asyncio.to_thread(
            subprocess.run, command, shell=True, check=True, capture_output=True, text=True
        )
        print(f"Successfully prepared {os.path.basename(output_path)}")
        return output_path
    except subprocess.CalledProcessError as e:
        print(f"FFmpeg copy error: {e.stderr}. Returning original.")
        return file_path
    except FileNotFoundError:
        print("FFmpeg command not found. Returning original file.")
        return file_path

async def upload_with_telethon(telethon_client: TelegramClient, bot: Bot, app_state: AppState, file_path: str, original_filename: str, info_hash_str: str) -> bool:
    last_update_time = 0
    async def progress_callback(current, total):
        nonlocal last_update_time
        now = time.time()
        if now - last_update_time > 5:
            percentage = current / total * 100
            await update_status_message(bot, app_state, info_hash_str, f"Uploading `{original_filename}` ({percentage:.1f}%)")
            last_update_time = now

    try:
        if not os.path.exists(file_path):
            print(f"Telethon: Upload failed, file not found: {file_path}")
            return False
        
        _, extension = os.path.splitext(original_filename)
        extension = extension.lower()
        attributes = []
        force_document = True
        metadata = await get_media_metadata(file_path) or {}
        duration = metadata.get('duration', 0)

        if extension in config.VIDEO_EXTENSIONS:
            force_document = False
            attributes.append(DocumentAttributeVideo(duration=duration, w=metadata.get('width', 0), h=metadata.get('height', 0), supports_streaming=True))
        elif extension in config.AUDIO_EXTENSIONS:
            force_document = False
            attributes.append(DocumentAttributeAudio(duration=duration, title=metadata.get('title'), performer=metadata.get('artist')))
        elif extension in config.IMAGE_EXTENSIONS:
            force_document = False

        print(f"Telethon: Starting upload for {original_filename} (as_document: {force_document})")
        await telethon_client.send_file(
            config.TARGET_CHAT_ID, file_path, caption=original_filename, 
            force_document=force_document, attributes=attributes, 
            workers=config.UPLOAD_WORKERS, progress_callback=progress_callback
        )
        print(f"Telethon: Successfully uploaded {original_filename}")
        
        filesize = os.path.getsize(file_path)
        fingerprint = (original_filename, filesize)
        # --- THIS IS THE CORRECTED LINE ---
        if fingerprint not in app_state.channel_file_index:
            app_state.channel_file_index.add(fingerprint)
            await save_fingerprint_to_disk(original_filename, filesize)
        # ------------------------------------
        
        return True
    except Exception as e:
        print(f"Telethon: Error uploading {file_path}: {e}")
        return False

async def uploader_worker(app, telethon_client: TelegramClient, app_state: AppState, session):
    while True:
        item = await app_state.upload_queue.get()
        try:
            info_hash_str = item["info_hash"]
            
            if info_hash_str not in app_state.torrent_locks:
                print(f"Lock not found for torrent {info_hash_str}, skipping orphaned file.")
                continue
            lock = app_state.torrent_locks[info_hash_str]

            async with lock:
                torrent_data = app_state.active_torrents.get(info_hash_str)
                if not torrent_data:
                    print(f"Orphaned file in upload queue, skipping: {os.path.basename(item['path'])}")
                    if item.get("is_extracted_content") and os.path.exists(item['path']):
                        os.remove(item['path'])
                    continue
            
            should_extract = item.get("extract", False)
            job_successful = False

            if should_extract:
                await update_status_message(app.bot, app_state, info_hash_str, f"Extracting `{os.path.basename(item['path'])}`...")
                await process_archive(app, app_state, item['path'], info_hash_str)
                job_successful = True
            else:
                job_successful = await process_single_file(app, telethon_client, app_state, item)

            async with lock:
                torrent_data = app_state.active_torrents.get(info_hash_str)
                if not torrent_data:
                    continue

                if job_successful:
                    torrent_data["jobs_completed"] += 1

                if torrent_data["jobs_completed"] >= torrent_data["jobs_total"]:
                    print(f"All jobs for torrent {info_hash_str} have been completed. Cleaning up...")
                    await update_status_message(app.bot, app_state, info_hash_str, f"✅ Finished! All files processed and uploaded.", is_final=True)
                    
                    handle = torrent_data["handle"]
                    if handle.is_valid():
                        session.remove_torrent(handle, session.delete_files)
                    
                    jobs = app.job_queue.get_jobs_by_name(f"job_{info_hash_str}")
                    for job in jobs: job.schedule_removal()
                    
                    if info_hash_str in app_state.torrent_metadata_cache:
                        temp_torrent_path = app_state.torrent_metadata_cache.pop(info_hash_str)
                        if os.path.exists(temp_torrent_path): os.remove(temp_torrent_path)
                    
                    if info_hash_str in app_state.torrent_locks:
                        del app_state.torrent_locks[info_hash_str]

                    del app_state.active_torrents[info_hash_str]

        except Exception as e:
            print(f"Error in uploader_worker for {item.get('path', 'N/A')}: {e}")
        finally:
            app_state.upload_queue.task_done()

async def process_single_file(app, telethon_client, app_state, item) -> bool:
    file_path = item["path"]
    info_hash_str = item["info_hash"]
    is_extracted_content = item.get("is_extracted_content", False)
    
    prepared_path = None
    upload_successful = False
    try:
        filename = os.path.basename(file_path)
        
        if not os.path.exists(file_path):
            await asyncio.sleep(2)
            if not os.path.exists(file_path):
                print(f"File still not found after delay, skipping: {filename}")
                return True

        await update_status_message(app.bot, app_state, info_hash_str, f"Checking `{filename}`...")
        filesize = os.path.getsize(file_path)
        
        if (filename, filesize) in app_state.channel_file_index:
            print(f"⏭️ Skipping duplicate file already in channel: {filename}")
            return True
        
        await update_status_message(app.bot, app_state, info_hash_str, f"Preparing `{filename}`...")
        
        path_to_upload = await prepare_file_for_upload(file_path)
        if path_to_upload != file_path:
            prepared_path = path_to_upload

        if not path_to_upload:
            raise Exception("File preparation failed.")

        upload_successful = await upload_with_telethon(
            telethon_client, app.bot, app_state, 
            path_to_upload, filename, info_hash_str
        )
        
        if not upload_successful:
            await update_status_message(app.bot, app_state, info_hash_str, f"⚠️ Upload failed for `{filename}`.")

    finally:
        if prepared_path and os.path.exists(prepared_path):
            os.remove(prepared_path)
        if is_extracted_content and os.path.exists(file_path):
            os.remove(file_path)
            try:
                os.rmdir(os.path.dirname(file_path))
            except OSError:
                pass
    
    return upload_successful

async def fetch_and_load_trackers():
    print("Fetching latest tracker lists...")
    all_trackers = set()
    async with aiohttp.ClientSession() as session:
        for url in config.TRACKER_URLS:
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        text = await response.text()
                        trackers = {tracker.strip() for tracker in text.split('\n') if tracker.strip()}
                        all_trackers.update(trackers)
                        print(f"Loaded {len(trackers)} trackers from {os.path.basename(url)}")
                    else:
                        print(f"Failed to fetch {os.path.basename(url)}. Status: {response.status}")
            except Exception as e:
                print(f"Error fetching {os.path.basename(url)}: {e}")
    config.PUBLIC_TRACKERS = list(all_trackers)
    print(f"Successfully loaded a total of {len(config.PUBLIC_TRACKERS)} unique trackers.")