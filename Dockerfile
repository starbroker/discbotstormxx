# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Install ffmpeg
RUN apt-get update && apt-get install -y ffmpeg

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

#
# THIS IS THE CRUCIAL LINE THAT FIXES YOUR ERROR
# It forces an upgrade to the latest yt-dlp, fixing the circular import bug
RUN pip install --upgrade yt-dlp
#
#

# Copy the bot script
COPY bot.py .

# Run bot.py when the container launches
CMD ["python", "bot.py"]
