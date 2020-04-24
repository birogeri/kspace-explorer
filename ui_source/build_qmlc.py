import subprocess
qrc_file = 'kspace.qrc'

# Building qrc.py file from resources list in kspace.qrc
pyrcc_path = "./pyrcc5.exe"
args = pyrcc_path + ' -o ' + 'qrc.py' + ' -compress 9 ' + qrc_file
subprocess.call(args, shell=False)

