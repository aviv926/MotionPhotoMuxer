import argparse
import logging
import os
import shutil
import sys
from os.path import exists, basename, isdir
from PIL import Image
import pyheif
import pyexiv2


def validate_directory(dir):
    
    if not exists(dir):
        logging.error("Path doesn't exist: {}".format(dir))
        exit(1)
    if not isdir(dir):
        logging.error("Path is not a directory: {}".format(dir))
        exit(1)

def validate_media(photo_path, video_path):
    """
    Checks if the files provided are valid inputs. Currently the only supported inputs are MP4/MOV and JPEG filetypes.
    Currently it only checks file extensions instead of actually checking file formats via file signature bytes.
    :param photo_path: path to the photo file
    :param video_path: path to the video file
    :return: True if photo and video files are valid, else False
    """
    if not exists(photo_path):
        logging.error("Photo does not exist: {}".format(photo_path))
        return False
    if not exists(video_path):
        logging.error("Video does not exist: {}".format(video_path))
        return False
    if not photo_path.lower().endswith(('.jpg', '.jpeg')):
        logging.error("Photo isn't a JPEG: {}".format(photo_path))
        return False
    if not video_path.lower().endswith(('.mov', '.mp4')):
        logging.error("Video isn't a MOV or MP4: {}".format(photo_path))
        return False
    return True

def merge_files(photo_path, video_path, output_path):
    """Merges the photo and video file together by concatenating the video at the end of the photo. Writes the output to
    a temporary folder.
    :param photo_path: Path to the photo
    :param video_path: Path to the video
    :return: File name of the merged output file
    """
    logging.info("Merging {} and {}.".format(photo_path, video_path))
    out_path = os.path.join(output_path, "{}".format(basename(photo_path)))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as outfile, open(photo_path, "rb") as photo, open(video_path, "rb") as video:
        outfile.write(photo.read())
        outfile.write(video.read())
    logging.info("Merged photo and video.")
    return out_path


def add_xmp_metadata(merged_file, offset):
    """Adds XMP metadata to the merged image indicating the byte offset in the file where the video begins.
    :param merged_file: The path to the file that has the photo and video merged together.
    :param offset: The number of bytes from EOF to the beginning of the video.
    :return: None
    """
    metadata = pyexiv2.ImageMetadata(merged_file)
    logging.info("Reading existing metadata from file.")
    metadata.read()
    logging.info("Found XMP keys: " + str(metadata.xmp_keys))
    if len(metadata.xmp_keys) > 0:
        logging.warning("Found existing XMP keys. They *may* be affected after this process.")

    # (py)exiv2 raises an exception here on basically all my 'test' iPhone 13 photos -- I'm not sure why,
    # but it seems safe to ignore so far. It's logged anyways just in case.
    try:
        pyexiv2.xmp.register_namespace('http://ns.google.com/photos/1.0/camera/', 'GCamera')
    except KeyError:
        logging.warning("exiv2 detected that the GCamera namespace already exists.".format(merged_file))
    metadata['Xmp.GCamera.MicroVideo'] = pyexiv2.XmpTag('Xmp.GCamera.MicroVideo', 1)
    metadata['Xmp.GCamera.MicroVideoVersion'] = pyexiv2.XmpTag('Xmp.GCamera.MicroVideoVersion', 1)
    metadata['Xmp.GCamera.MicroVideoOffset'] = pyexiv2.XmpTag('Xmp.GCamera.MicroVideoOffset', offset)
    metadata['Xmp.GCamera.MicroVideoPresentationTimestampUs'] = pyexiv2.XmpTag(
        'Xmp.GCamera.MicroVideoPresentationTimestampUs',
        1500000)  # in Apple Live Photos, the chosen photo is 1.5s after the start of the video, so 1500000 microseconds
    metadata.write()


def convert(photo_path, video_path, output_path, convert_all=False):
    """
    Performs the conversion process to mux the files together into a Google Motion Photo.
    :param photo_path: path to the photo to merge
    :param video_path: path to the video to merge
    :param convert_all: if True, convert all HEIC files to JPEG regardless of video presence and size
    :return: True if conversion was successful, else False
    """
    if not convert_all:
        video_size_limit_mb = 10  # Set the video size limit to 10MB
        if not os.path.exists(video_path) or os.path.getsize(video_path) > video_size_limit_mb * 1024 * 1024:
            logging.warning("Skipping conversion of {} due to missing or large video file.".format(photo_path))
            return False

    if photo_path.lower().endswith('.heic'):
        # Convert HEIC to JPEG
        with open(photo_path, 'rb') as f:
            heif_file = pyheif.read(f)
            image = Image.frombytes(
                heif_file.mode, 
                heif_file.size, 
                heif_file.data,
                "raw",
                heif_file.mode,
                heif_file.stride,
            )
            jpeg_path = os.path.splitext(photo_path)[0] + '.jpg'
            image.save(jpeg_path, "JPEG", quality=100, exif=heif_file.metadata)

        photo_path = jpeg_path

    merged = merge_files(photo_path, video_path, output_path)
    photo_filesize = os.path.getsize(photo_path)
    merged_filesize = os.path.getsize(merged)

    # The 'offset' field in the XMP metadata should be the offset (in bytes) from the end of the file to the part
    # where the video portion of the merged file begins. In other words, merged size - photo_only_size = offset.
    offset = merged_filesize - photo_filesize
    add_xmp_metadata(merged, offset)


def matching_video(photo_path):
    base = os.path.splitext(photo_path)[0]
    logging.info("Looking for videos named: {}".format(base))
    for ext in ('.mov', '.mp4', '.MOV', '.MP4'):
        video_path = base + ext
        if os.path.exists(video_path):
            return video_path
    return ""


def process_directory(file_dir, recurse):
    """
    Loops through files in the specified directory and generates a list of (photo, video) path tuples that can
    be converted
    :param file_dir: directory to look for photos/videos to convert
    :param recurse: if true, subdirectories will recursively be processes
    :return: a list of tuples containing matched photo/video pairs.
    """
    logging.info("Processing dir: {}".format(file_dir))
    file_pairs = []
    for root, dirs, files in os.walk(file_dir):
        for file in files:
            file_fullpath = os.path.join(root, file)
            if os.path.isfile(file_fullpath):
                base_name, ext = os.path.splitext(file)
                if ext.lower() in ('.jpg', '.jpeg', '.heic'):
                    video_path = matching_video(file_fullpath)
                    if video_path:
                        file_pairs.append((file_fullpath, video_path))

    logging.info("Found {} pairs.".format(len(file_pairs)))
    logging.info("Subset of found image/video pairs: {}".format(str(file_pairs[:min(10, len(file_pairs))])))
    return file_pairs



def main():
    logging_level = logging.INFO
    logging.basicConfig(level=logging_level, stream=sys.stdout)
    logging.info("Enabled verbose logging")

    source_dir = get_source_directory()
    out_dir = get_destination_directory()
    convert_all = ask_convert_all()

    pairs = process_directory(source_dir, recurse=False)  # Fix the missing argument
    processed_files = set()
    for pair in pairs:
        if validate_media(pair[0], pair[1]):
            convert(pair[0], pair[1], out_dir, convert_all)
            processed_files.add(pair[0])
            processed_files.add(pair[1])

    if ask_copy_all():
        # Populate remaining_files if the user chooses to copy unprocessed files
        all_files = set(os.path.join(source_dir, file) for file in os.listdir(source_dir))
        remaining_files = all_files - processed_files

        logging.info("Found {} remaining files that will be copied.".format(len(remaining_files)))

        if len(remaining_files) > 0:
            # Ensure the destination directory exists
            os.makedirs(out_dir, exist_ok=True)
            
            for file in remaining_files:
                if os.path.isfile(file):  # Check if it's a file before copying
                    file_name = os.path.basename(file)
                    destination_path = os.path.join(out_dir, file_name)
                    shutil.copy2(file, destination_path)



    delete_converted_files = ask_delete_converted_files()
    if delete_converted_files:
        delete_files(processed_files)

def ask_delete_converted_files():
    """Ask the user if they want to delete converted files."""
    while True:
        choice = input("Do you want to delete converted files (HEIC and video files)? (yes/no): ").strip().lower()
        if choice in ('yes', 'no'):
            return choice == 'yes'
        else:
            print("Please enter 'yes' or 'no'.")

def delete_files(files):
    """Delete the specified files."""
    for file in files:
        try:
            os.remove(file)
            logging.info("Deleted file: {}".format(file))
        except Exception as e:
            logging.error("Error deleting file '{}': {}".format(file, str(e)))


def get_source_directory():
    """Prompt the user to input the source directory."""
    while True:
        source_dir = input("Enter the source directory: ").strip()
        if not os.path.isdir(source_dir):
            print("Error: '{}' is not a valid directory.".format(source_dir))
        else:
            return source_dir

def get_destination_directory():
    """Prompt the user to input the destination directory."""
    while True:
        dest_dir = input("Enter the destination directory: ").strip()
        if not os.path.isdir(dest_dir):
            print("Error: '{}' is not a valid directory.".format(dest_dir))
        else:
            return dest_dir

def ask_convert_all():
    """Ask the user if they want to convert all HEIC files to JPEG."""
    while True:
        choice = input("Do you want to convert all HEIC files to JPEG? (yes/no): ").strip().lower()
        if choice in ('yes', 'no'):
            return choice == 'yes'
        else:
            print("Please enter 'yes' or 'no'.")

def ask_copy_all():
    """Ask the user if they want to copy all unprocessed files to the destination directory."""
    while True:
        choice = input("Do you want to copy all unprocessed files to the destination directory? (yes/no): ").strip().lower()
        if choice in ('yes', 'no'):
            return choice == 'yes'
        else:
            print("Please enter 'yes' or 'no'.")

if __name__ == '__main__':
    main()
