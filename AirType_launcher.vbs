' AirType_launcher.vbs
' AirType をコンソールウィンドウなしでバックグラウンド起動するランチャー。
' このファイルを main.py と同じフォルダに置いてダブルクリックするだけで起動できます。
'
' 仮想環境の優先順位:
'   1. .\venv\Scripts\pythonw.exe   (.venv という名前)
'   2. .\venv\Scripts\pythonw.exe   (venv  という名前)
'   3. システムの pythonw.exe        (PATH から検索)
'
' ログは airtype.log (main.py と同じフォルダ) に出力されます。

Option Explicit

Dim fso, wsh, dir, python, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set wsh = CreateObject("WScript.Shell")

' このスクリプトと同じフォルダを作業ディレクトリにする
dir = fso.GetParentFolderName(WScript.ScriptFullName)
wsh.CurrentDirectory = dir

' 仮想環境の pythonw.exe を探す (コンソールなし起動)
If fso.FileExists(dir & "\.venv\Scripts\pythonw.exe") Then
    python = Chr(34) & dir & "\.venv\Scripts\pythonw.exe" & Chr(34)
ElseIf fso.FileExists(dir & "\venv\Scripts\pythonw.exe") Then
    python = Chr(34) & dir & "\venv\Scripts\pythonw.exe" & Chr(34)
Else
    python = "pythonw.exe"
End If

' main.py が存在するか確認
If Not fso.FileExists(dir & "\main.py") Then
    MsgBox "main.py が見つかりません:" & vbCrLf & dir & "\main.py", vbCritical, "AirType"
    WScript.Quit 1
End If

' バックグラウンドで起動 (第2引数 0 = ウィンドウ非表示, 第3引数 False = 非同期)
cmd = python & " " & Chr(34) & dir & "\main.py" & Chr(34)
wsh.Run cmd, 0, False

Set fso = Nothing
Set wsh = Nothing
