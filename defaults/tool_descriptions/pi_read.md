Read the contents of a file. Supports text files and images (jpg, png, gif, webp, bmp). Images are sent as attachments. For text files, output is truncated to 2000 lines or 50KB (whichever is hit first). Use offset/limit for large files. When you need the full file, continue with offset until complete.

<!--parameters-->

### path

Path to the file to read (relative or absolute)

### offset

Line number to start reading from (1-indexed)

### limit

Maximum number of lines to read
