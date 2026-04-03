;; Take screenshot of current window (requires savePath parameter)
procedure(takeScreenshot(savePath)
    let((window)
        window = hiGetCurrentWindow()
        if(window then
            hiWindowSaveImage(
                ?target window
                ?path savePath
                ?format "png"
                ?toplevel nil
            )
            sprintf(nil "Screenshot saved to: %s" savePath)
        else
            sprintf(nil "Error: No active window found")
        )
    )
)
