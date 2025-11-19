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
import re
import glob

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeAudio
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

import config
from state import AppState

INDEX_FILE = "channel_index.json"
MAX_FILE_SIZE_BYTES = 2000 * 1024 * 1024 # 2000 MB safe limit

# ... (All functions from the top down to split_large_file are unchanged) ...
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

async def refresh_status_panel(bot: Bot, app_state: AppState, info_hash_str: str, current_task: str, is_final: bool = False):
    torrent_data = app_state.active_torrents.get(info_hash_str)
    if not torrent_data or not torrent_data.get('user_chat_id') or not torrent_data.get('status_message_id'):
        return

    handle = torrent_data["handle"]
    if not handle.is_valid(): return
    
    status = handle.status()
    info = handle.torrent_file()
    if not info: return

    keyboard = []
    if is_final:
        message = f"✅ **Finished:** `{info.name()}` has been successfully processed and uploaded."
    else:
        progress_percent = status.progress
        progress_bar = create_progress_bar(progress_percent)
        
        state_str = STATE_MAP.get(status.state, 'N/A')
        state_emoji = "🚀" if state_str == "Downloading" else "⚙️"
        
        message = (
            f"**Torrent:** `{info.name()}`\n\n"
            f"**[ {state_emoji} {state_str} ]** {progress_bar} {progress_percent * 100:.1f}%\n\n"
            f"> {current_task}"
        )

        details_visible = torrent_data.get("details_visible", False)
        if details_visible:
            download_speed = format_bytes(status.download_rate) + '/s'
            upload_speed = format_bytes(status.upload_rate) + '/s'
            total_wanted = status.total_wanted
            eta_seconds = (total_wanted - status.total_download) / status.download_rate if status.download_rate > 0 else float('inf')
            eta_str = format_time(eta_seconds)
            jobs_done = torrent_data['jobs_completed']
            jobs_total = torrent_data['jobs_total']

            details_text = (
                f"\n\n**📊 Stats**\n"
                f" D-Speed: {download_speed}\n"
                f" U-Speed: {upload_speed}\n"
                f" Peers: {status.num_peers}\n"
                f" ETA: {eta_str}\n\n"
                f"**📈 Progress**\n"
                f" Jobs: {jobs_done} / {jobs_total}"
            )
            message += details_text
            details_button = InlineKeyboardButton("🔼 Hide Details", callback_data=f"details_{info_hash_str}")
        else:
            details_button = InlineKeyboardButton("🔽 Show Details", callback_data=f"details_{info_hash_str}")
        
        cancel_button = InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{info_hash_str}")
        keyboard.append([details_button, cancel_button])

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    try:
        await bot.edit_message_text(
            chat_id=torrent_data['user_chat_id'], 
            message_id=torrent_data['status_message_id'], 
            text=message, 
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
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

async def process_archive(app, app_state: AppState, archive_path: str, info_hash_str: str) -> list[str]:
    """Extracts an archive and returns a list of extracted file paths."""
    torrent_data = app_state.active_torrents.get(info_hash_str)
    if not torrent_data: return []

    extract_dir = os.path.join("temp", str(uuid.uuid4()))
    extracted_files = []
    
    try:
        os.makedirs(extract_dir, exist_ok=True)
        success = await asyncio.to_thread(_extract_sync, archive_path, extract_dir)
        
        if success:
            for root, _, files in os.walk(extract_dir):
                for file in files:
                    extracted_files.append(os.path.join(root, file))
            
            # Recursively handle nested archives
            final_files = []
            for file_path in extracted_files:
                if file_path.lower().endswith(config.ARCHIVE_EXTENSIONS):
                    await refresh_status_panel(app.bot, app_state, info_hash_str, f"Extracting nested `{os.path.basename(file_path)}`...")
                    nested_files = await process_archive(app, app_state, file_path, info_hash_str)
                    final_files.extend(nested_files)
                else:
                    final_files.append(file_path)
            
            return final_files
        else:
            await refresh_status_panel(app.bot, app_state, info_hash_str, f"ℹ️ Archive `{os.path.basename(archive_path)}` was empty or failed.")
            return []

    finally:
        if os.path.exists(archive_path):
            os.remove(archive_path)

async def get_media_metadata(file_path: str) -> dict | None:
    try:
        command = ['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', file_path]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            return None

        data = json.loads(stdout.decode())
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

async def run_ffmpeg_command(app, app_state, info_hash_str, filename, command: list, timeout: int, total_duration: float) -> int:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    last_update_time = 0

    async def drain_stderr(pipe):
        while True:
            line = await pipe.readline()
            if not line: break

    async def monitor_progress(pipe):
        nonlocal last_update_time
        progress_data = {}
        while True:
            line = await pipe.readline()
            if not line: break
            line = line.decode().strip()
            if '=' in line:
                key, value = line.split('=', 1)
                progress_data[key.strip()] = value.strip()
                if key.strip() == 'progress' and value.strip() == 'end': break
                if key.strip() == 'out_time_ms':
                    now = time.time()
                    if now - last_update_time > 5:
                        last_update_time = now
                        current_time = float(value) / 1_000_000
                        if total_duration > 0:
                            percent = (current_time / total_duration) * 100
                            await refresh_status_panel(app.bot, app_state, info_hash_str, f"Re-encoding `{filename}` ({percent:.1f}%)")

    drain_stderr_task = asyncio.create_task(drain_stderr(process.stderr))
    monitor_progress_task = asyncio.create_task(monitor_progress(process.stdout))

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"FFmpeg process timed out after {timeout} seconds. Terminating...")
        process.terminate()
        await process.wait()
        return -1
    finally:
        drain_stderr_task.cancel()
        monitor_progress_task.cancel()

    return process.returncode

async def prepare_file_for_upload(app, app_state, info_hash_str, file_path: str) -> str | None:
    _, extension = os.path.splitext(file_path)
    extension = extension.lower()
    if extension not in config.VIDEO_EXTENSIONS:
        return file_path
    
    print(f"Preparing video for streaming: {os.path.basename(file_path)}")
    
    output_path = os.path.join("downloads", ".transcode_temp", f"{uuid.uuid4()}.mp4")
    path_to_return = file_path
    
    try:
        metadata = await get_media_metadata(file_path)
        total_duration = metadata.get('duration', 0.0) if metadata else 0.0

        command_fast = [
            'ffmpeg', '-nostdin', '-i', file_path, '-y',
            '-c:v', 'copy', '-c:a', 'aac', '-movflags', '+faststart',
            '-pix_fmt', 'yuv420p',
            output_path
        ]
        return_code_fast = await run_ffmpeg_command(app, app_state, info_hash_str, os.path.basename(file_path), command_fast, timeout=1800, total_duration=0)

        if return_code_fast == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            print(f"Successfully prepared (fast mode): {os.path.basename(output_path)}")
            path_to_return = output_path
        else:
            print(f"Fast preparation failed. Falling back to full re-encoding (this may be slow)...")
            
            command_slow = [
                'ffmpeg', '-nostdin', '-i', file_path, '-y',
                '-progress', 'pipe:1',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-c:a', 'aac', '-movflags', '+faststart',
                '-pix_fmt', 'yuv420p',
                output_path
            ]
            return_code_slow = await run_ffmpeg_command(app, app_state, info_hash_str, os.path.basename(file_path), command_slow, timeout=10800, total_duration=total_duration)

            if return_code_slow == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                print(f"Successfully prepared (slow mode): {os.path.basename(output_path)}")
                path_to_return = output_path
            else:
                print(f"Full re-encoding also failed. Uploading original file.")
                if os.path.exists(output_path):
                    os.remove(output_path)
                path_to_return = file_path
    
    except Exception as e:
        print(f"A critical error occurred during FFmpeg processing: {e}. Uploading original file.")
        path_to_return = file_path

    return path_to_return

async def upload_with_telethon(telethon_client: TelegramClient, bot: Bot, app_state: AppState, file_path: str, original_filename: str, info_hash_str: str) -> bool:
    last_update_time = 0
    async def progress_callback(current, total):
        nonlocal last_update_time
        now = time.time()
        if now - last_update_time > 5:
            percentage = current / total * 100
            await refresh_status_panel(bot, app_state, info_hash_str, f"Uploading `{original_filename}` ({percentage:.1f}%)")
            last_update_time = now

    try:
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            print(f"Telethon: Upload failed, file is missing or zero-byte: {file_path}")
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
            config.TARGET_CHAT_ID, 
            file_path, 
            caption=original_filename, 
            force_document=force_document, 
            attributes=attributes, 
            workers=config.TELETHON_UPLOAD_WORKERS, 
            part_size_kb=config.TELETHON_PART_SIZE_KB,
            progress_callback=progress_callback
        )
        print(f"Telethon: Successfully uploaded {original_filename}")
        
        filesize = os.path.getsize(file_path)
        fingerprint = (original_filename, filesize)
        if fingerprint not in app_state.channel_file_index:
            app_state.channel_file_index.add(fingerprint)
            await save_fingerprint_to_disk(original_filename, filesize)
        
        return True
    except Exception as e:
        print(f"Telethon: Error uploading {file_path}: {e}")
        return False

def _split_file_sync(file_path, split_dir, chunk_size, max_size, progress_callback):
    try:
        base_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        part_num = 1
        bytes_read_total = 0
        parts = []
        last_update_time = 0

        with open(file_path, 'rb') as infile:
            while True:
                part_filename = f"{base_name}.{part_num:03d}"
                part_path = os.path.join(split_dir, part_filename)
                current_part_size = 0
                
                with open(part_path, 'wb') as outfile:
                    while current_part_size < max_size:
                        chunk = infile.read(chunk_size)
                        if not chunk:
                            break
                        outfile.write(chunk)
                        current_part_size += len(chunk)
                        bytes_read_total += len(chunk)
                        
                        now = time.time()
                        if now - last_update_time > 5:
                            last_update_time = now
                            percent = (bytes_read_total / file_size) * 100
                            progress_callback(percent)

                if current_part_size > 0:
                    parts.append(part_path)
                    part_num += 1
                
                if infile.tell() >= file_size:
                    break
        return parts
    except Exception as e:
        print(f"Error in sync splitter: {e}")
        return []

async def split_large_file(app, app_state, info_hash_str, file_path: str) -> list[str]:
    print(f"Splitting large file: {os.path.basename(file_path)}")
    
    split_dir = os.path.join("downloads", ".transcode_temp", f"split_{uuid.uuid4()}")
    os.makedirs(split_dir, exist_ok=True)
    
    base_name = os.path.basename(file_path)
    
    loop = asyncio.get_running_loop()
    def progress_callback(percent):
        asyncio.run_coroutine_threadsafe(
            refresh_status_panel(app.bot, app_state, info_hash_str, f"Splitting `{base_name}` ({percent:.1f}%)"),
            loop
        )

    parts = await asyncio.to_thread(
        _split_file_sync, 
        file_path, 
        split_dir, 
        10 * 1024 * 1024, 
        MAX_FILE_SIZE_BYTES, 
        progress_callback
    )
    
    if parts:
        print(f"Successfully split into {len(parts)} parts.")
    else:
        print("Split failed.")
        
    return parts

# --- FIX: Pass the session object to flush_upload_buffer ---
async def flush_upload_buffer(app, telethon_client, app_state, info_hash_str, session):
    if info_hash_str not in app_state.torrent_locks: return
    lock = app_state.torrent_locks[info_hash_str]

    async with lock:
        torrent_data = app_state.active_torrents.get(info_hash_str)
        if not torrent_data: return

        while True:
            current_idx_ptr = torrent_data["current_upload_idx"]
            
            if current_idx_ptr >= len(torrent_data["upload_order"]):
                break
            
            file_index = torrent_data["upload_order"][current_idx_ptr]
            
            if file_index in torrent_data["ready_buffer"]:
                prepared_files = torrent_data["ready_buffer"].pop(file_index)
                
                for path in prepared_files:
                    filename = os.path.basename(path)
                    await upload_with_telethon(telethon_client, app.bot, app_state, path, filename, info_hash_str)
                    
                    if os.path.exists(path):
                        os.remove(path)
                        try:
                            os.rmdir(os.path.dirname(path))
                        except OSError:
                            pass

                torrent_data["current_upload_idx"] += 1
                torrent_data["jobs_completed"] += 1
            else:
                break

        if torrent_data["jobs_completed"] >= torrent_data["jobs_total"]:
            print(f"All jobs for torrent {info_hash_str} have been completed. Cleaning up...")
            await refresh_status_panel(app.bot, app_state, info_hash_str, "", is_final=True)
            
            handle = torrent_data["handle"]
            if handle.is_valid():
                # --- FIX: Use the passed session object ---
                session.remove_torrent(handle, session.delete_files)
            
            jobs = app.job_queue.get_jobs_by_name(f"job_{info_hash_str}")
            for job in jobs: job.schedule_removal()
            
            if info_hash_str in app_state.torrent_metadata_cache:
                temp_torrent_path = app_state.torrent_metadata_cache.pop(info_hash_str)
                if os.path.exists(temp_torrent_path): os.remove(temp_torrent_path)
            
            del app_state.active_torrents[info_hash_str]

async def uploader_worker(app, telethon_client: TelegramClient, app_state: AppState, session):
    while True:
        item = await app_state.upload_queue.get()
        try:
            info_hash_str = item["info_hash"]
            file_index = item.get("file_index")
            
            if info_hash_str not in app_state.torrent_locks:
                continue

            torrent_data = app_state.active_torrents.get(info_hash_str)
            if not torrent_data:
                if item.get("is_extracted_content") and os.path.exists(item['path']):
                    os.remove(item['path'])
                app_state.upload_queue.task_done()
                continue

            should_extract = item.get("extract", False)
            prepared_files = []

            if should_extract:
                await refresh_status_panel(app.bot, app_state, info_hash_str, f"Extracting `{os.path.basename(item['path'])}`...")
                prepared_files = await process_archive(app, app_state, item['path'], info_hash_str)
                
                final_ready_files = []
                for p in prepared_files:
                    if os.path.getsize(p) > MAX_FILE_SIZE_BYTES:
                         parts = await split_large_file(app, app_state, info_hash_str, p)
                         final_ready_files.extend(parts)
                         os.remove(p)
                    else:
                         ready_p = await prepare_file_for_upload(app, app_state, info_hash_str, p)
                         final_ready_files.append(ready_p)
                prepared_files = final_ready_files

            else:
                await refresh_status_panel(app.bot, app_state, info_hash_str, f"Preparing `{os.path.basename(item['path'])}`...")
                
                path_to_upload = await prepare_file_for_upload(app, app_state, info_hash_str, item['path'])
                
                if os.path.getsize(path_to_upload) > MAX_FILE_SIZE_BYTES:
                    await refresh_status_panel(app.bot, app_state, info_hash_str, f"Splitting `{os.path.basename(path_to_upload)}`...")
                    prepared_files = await split_large_file(app, app_state, info_hash_str, path_to_upload)
                    if path_to_upload != item['path']: os.remove(path_to_upload)
                else:
                    prepared_files = [path_to_upload]

            async with app_state.torrent_locks[info_hash_str]:
                torrent_data = app_state.active_torrents.get(info_hash_str)
                if torrent_data:
                    torrent_data["ready_buffer"][file_index] = prepared_files
            
            # --- FIX: Pass the session object ---
            await flush_upload_buffer(app, telethon_client, app_state, info_hash_str, session)

        except Exception as e:
            print(f"Error in uploader_worker for {item.get('path', 'N/A')}: {e}")
        finally:
            app_state.upload_queue.task_done()

async def fetch_and_load_trackers():
    print("Fetching latest tracker lists...")
    all_trackers = set()
    for url in config.TRACKER_URLS:
        try:
            async with aiohttp.ClientSession() as session:
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
