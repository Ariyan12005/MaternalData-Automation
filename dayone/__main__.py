from dayone.server import main

# Guarded: OCR processes are spawned (dayone/ocr_process.py) and re-import this module without being the server.
if __name__ == "__main__":
    main()
