Option Explicit

Dim shell, fso, root, script
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(root, "launcher\bin\GPTBridge.Bootstrap.exe")
If Not fso.FileExists(script) Then
    WScript.Quit 1
End If
shell.CurrentDirectory = root
shell.Run """" & script & """ --project-root """ & root & """", 0, False
