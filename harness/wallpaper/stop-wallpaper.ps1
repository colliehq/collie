try { [System.Threading.EventWaitHandle]::OpenExisting('collie-wallpaper-quit').Set(); 'signalled clean shutdown' }
catch { 'no running collie-wallpaper (or no quit channel)' }
