# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Install ffmpeg
# This is the crucial step for any audio bot
RUN apt-get update && apt-get install -y ffmpeg

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot script
COPY bot.py .

# Run bot.py when the container launches
CMD ["python", "bot.py"]
