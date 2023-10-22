import argparse
import parfive
from pathlib import Path
from typing import List, Optional, Union, TypedDict
from parfive import Results
import requests
import subprocess
import re

# Filelist type


class FileListDict(TypedDict):
    # Each str is like:
    # https://commadata2.blob.core.windows.net/commadata2/a2a0ccea32023010/2023-07-27--13-01-19/0/fcamera.hevc?se=2023-09-24T04%3A17%3A36Z&sp=r&sv=2018-03-28&sr=b&rscd=attachment%3B%20filename%3Da2a0ccea32023010_2023-07-27--13-01-19--0--fcamera.hevc&sig=a2oLhLvbKY7zlqTbyTmCVOjcN4Is1wQlaSUlZz1wK5U%3D

    # Filename are `fcamera.hevc`
    cameras: List[str]
    # Filename are `dcamera.hevc`
    dcameras: List[str]
    # Filename are `ecamera.hevc`
    ecameras: List[str]
    # Filename are `rlog.bz2`
    logs: List[str]

class RouteInfoDict(TypedDict):
    segment_end_times: List[int]
    segment_start_times: List[int]

def downloadSegments(
    data_dir: Union[str, Path],
    route_or_segment: str,
    smear_seconds: int,
    start_seconds: int,
    length: int,
    file_types: Optional[List[str]] = [
        "cameras",
        "ecameras",
        "logs",
    ],
):
    """
    Handle downloading segments and throwing up errors if something goes wrong.

    Also pre-decompresses the logs for performance reasons.

    """
    # Validate file_types are valid
    valid_file_types = ["cameras", "ecameras", "dcameras", "logs"]
    for file_type in file_types:
        if file_type not in valid_file_types:
            raise ValueError(f"Invalid file type argument: {file_type}. Valid file types are {valid_file_types}")

    # Get the route/segment name from the route/segment ID.
    # Just strip off the segment ID if it exists with regex
    # Examples:
    # a2a0ccea32023010|2023-07-27--13-01-19 -> a2a0ccea32023010|2023-07-27--13-01-19
    # a2a0ccea32023010|2023-07-27--13-01-19--5 -> a2a0ccea32023010|2023-07-27--13-01-19
    route = re.sub(r"--\d+$", "", route_or_segment)
    # Dongle ID is the part before the |
    dongle_id = route.split("|")[0]

    # Figure out which segments we're going to be downloading. Think of it like a sliding window that needs to cover minutes.
    # Segments start from index 0 and are 60 seconds long
    # Examples:
    # Start time: 0, length: 60 -> segment 0
    # Start time: 10, length: 60 -> segments 0 and 1
    # Start time: 400, length: 60 -> segments 6 and 7
    actual_start_seconds = max(0, start_seconds - smear_seconds)
    start_segment = actual_start_seconds // 60
    end_segment = (start_seconds + length) // 60
    segment_ids = list(range(start_segment, end_segment + 1))

    # Get file list JSON from https://api.commadotai.com/v1/route/<route>/files
    # E.g https://api.commadotai.com/v1/route/a2a0ccea32023010|2023-07-27--13-01-19/files
    # Make route URL encoded
    route_url = route.replace("|", "%7C")
    filelist_url = f"https://api.commadotai.com/v1/route/{route_url}/files"
    print(f"Downloading file list from {filelist_url}")

    # Check if the route is accessible
    # If it isn't, throw an error
    route_files_response = requests.get(filelist_url)
    if route_files_response.status_code != 200:
        raise ValueError(f"Route {route} is not accessible. You may need to set the route to be public. Visit https://connect.comma.ai/{dongle_id}, view the route, dropdown the \"More Info\" button, and toggle \"Public\". You can set \"Public\" back to off after using this tool.")
    filelist: FileListDict = route_files_response.json()

    # Get beginning and end times of the route for error message reasons
    # Find the segment_start_time and segment_end_time with the first and last segment_id
    routeinfo_url = f"https://api.commadotai.com/v1/route/{route_url}"
    print(f"Downloading route info from {routeinfo_url}")
    route_info_response = requests.get(routeinfo_url)
    route_info: RouteInfoDict = route_info_response.json()
    route_start_time = route_info["segment_start_times"][start_segment]
    route_end_time = route_info["segment_end_times"][end_segment]
    print(f"Route {route} starts at {route_start_time} and ends at {route_end_time}")
    comma_connect_url = f"https://connect.comma.ai/{dongle_id}/{route_start_time}/{route_end_time}"
    print(f"View the route at {comma_connect_url}")

    call_to_action_upload_message = f"Visit {comma_connect_url} , dropdown the \"Files\" button, and next to \"All files\", select \"Upload ## Files\". After all files have completed uploading, try again."

    # For every segment_id check if the file exists in the filelist
    # If it doesn't, throw an error
    for segment_id in segment_ids:
        camera_exists = False
        ecamera_exists = False
        dcamera_exists = False
        log_exists = False
        for camera_url in filelist["cameras"]:
            if f"/{segment_id}/fcamera.hevc" in camera_url:
                camera_exists = True
                break
        for ecamera_url in filelist["ecameras"]:
            if f"/{segment_id}/ecamera.hevc" in ecamera_url:
                ecamera_exists = True
                break
        for dcamera_url in filelist["dcameras"]:
            if f"/{segment_id}/dcamera.hevc" in dcamera_url:
                dcamera_exists = True
                break
        for log_url in filelist["logs"]:
            if f"/{segment_id}/rlog.bz2" in log_url:
                log_exists = True
                break
        if not camera_exists and "cameras" in file_types:
            raise ValueError(
                f"Segment {segment_id} does not have a forward camera upload. {call_to_action_upload_message}"
            )
        if not ecamera_exists and "ecameras" in file_types:
            raise ValueError(f"Segment {segment_id} does not have a wide camera upload. {call_to_action_upload_message}")
        if not dcamera_exists and "dcameras" in file_types:
            raise ValueError(
                f"Segment {segment_id} does not have a driver camera upload. {call_to_action_upload_message}"
            )
        if not log_exists and "logs" in file_types:
            raise ValueError(f"Segment {segment_id} does not have a log upload. {call_to_action_upload_message}")


    # Download the files
    # We use parfive to download the files
    # https://parfive.readthedocs.io/en/latest/
    #
    # We download the files to the data_dir
    # We find the corresponding URL in the filelist, and download it to the data_dir
    # E.g. https://commadata2.blob.core.windows.net/commadata2/a2a0ccea32023010/2023-07-27--13-01-19/0/fcamera.hevc?se=2023-09-24T04%3A17%3A36Z&sp=r&sv=2018-03-28&sr=b&rscd=attachment%3B%20filename%3Da2a0ccea32023010_2023-07-27--13-01-19--0--fcamera.hevc&sig=a2oLhLvbKY7zlqTbyTmCVOjcN4Is1wQlaSUlZz1wK5U%3D -> data_dir/a2a0ccea32023010_2023-07-27--13-01-19/0/fcamera.hevc

    # Make the date directory. It's just the route but with the ID stripped off the front.
    # E.g. a2a0ccea32023010|2023-07-27--13-01-19 -> 2023-07-27--13-01-19
    route_date = re.sub(r"^[^|]+\|", "", route)

    # Generate the list of URLs and paths to download to
    downloader = parfive.Downloader(
        max_conn=20,
    )

    # Download the data
    for segment_id in segment_ids:
        segment_dir = Path(data_dir) / f"{route_date}--{segment_id}"
        # Download the forward camera
        for camera_url in filelist["cameras"]:
            if f"/{segment_id}/fcamera.hevc" in camera_url and "cameras" in file_types:
                downloader.enqueue_file(
                    camera_url, path=segment_dir, filename="fcamera.hevc", overwrite=False
                )
                break
        # Download the wide camera
        for ecamera_url in filelist["ecameras"]:
            if f"/{segment_id}/ecamera.hevc" in ecamera_url and "ecameras" in file_types:
                downloader.enqueue_file(
                    ecamera_url, path=segment_dir, filename="ecamera.hevc", overwrite=False
                )
                break
        # Download the driver camera
        for dcamera_url in filelist["dcameras"]:
            if f"/{segment_id}/dcamera.hevc" in dcamera_url and "dcameras" in file_types:
                downloader.enqueue_file(
                    dcamera_url, path=segment_dir, filename="dcamera.hevc", overwrite=False
                )
                break
        # Download the log
        for log_url in filelist["logs"]:
            if f"/{segment_id}/rlog.bz2" in log_url and "logs" in file_types:
                # Check if the file already exists
                if (segment_dir / "rlog.bz2").exists() or (
                    segment_dir / "rlog"
                ).exists():
                    print(f"Skipping {log_url} because it already exists")
                    break
                downloader.enqueue_file(log_url, path=segment_dir, filename="rlog.bz2")
                break

    # Start the download
    results: Results = downloader.download()
    # Assume that the download is done when the results are done
    # Check if the download was successful
    if results.errors:
        raise ValueError(f"Download failed: {results.errors}")

    # Decompress the logs
    for segment_id in segment_ids:
        if "logs" not in file_types:
            break
        segment_dir = Path(data_dir) / f"{route_date}--{segment_id}"
        # Decompress the log if rlog doesn't exist
        if (segment_dir / "rlog").exists():
            print(f"Skipping decompression of {segment_id} because it already exists")
            continue
        log_path = segment_dir / "rlog.bz2"
        if log_path.exists():
            subprocess.run(["bzip2", "-d", log_path])
        else:
            raise ValueError(f"Segment {segment_id} does not have a log upload")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download openpilot routes/segments.")
    parser.add_argument("data_dir", type=str, help="Directory to download files to")
    parser.add_argument(
        "route_or_segment", type=str, help="Name of the route or segment to download"
    )
    parser.add_argument(
        "smear_seconds", type=int, help="Number of seconds to smear the start time"
    )
    parser.add_argument("start_seconds", type=int, help="Start time in seconds")
    parser.add_argument("length", type=int, help="Length of the segment to download")
    parser.add_argument(
        "--file_types",
        type=str,
        nargs="+",
        help="List of file types to download",
        default=["cameras", "ecameras", "logs"],
    )
    args = parser.parse_args()
    # All arguments are required

    downloadSegments(
        args.data_dir,
        args.route_or_segment,
        args.smear_seconds,
        args.start_seconds,
        args.length,
        args.file_types,
    )
