import sys

if __name__ == "__main__":
    if sys.argv[1:] == ["--render-thumbnail"]:
        try:
            from duplicate_cleaner.thumbnails import render_main
            result = render_main()
        except Exception:
            result = 1
        raise SystemExit(result)

    if sys.argv[1:] == ["--render-preview"]:
        from duplicate_cleaner.preview import render_main
        raise SystemExit(render_main())

    if sys.argv[1:] == ["--image-fingerprint"]:
        from duplicate_cleaner.similarity import fingerprint_main
        raise SystemExit(fingerprint_main())

    from duplicate_cleaner.gui import main

    raise SystemExit(main())
