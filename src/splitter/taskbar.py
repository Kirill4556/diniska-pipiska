"""Прогресс на значке в панели задач Windows (ITaskbarList3 через ctypes). На других ОС ничего не делает."""

import sys

NONE, INDETERMINATE, NORMAL, ERROR, PAUSED = 0, 1, 2, 4, 8

_CLSID_TASKBAR_LIST = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
_IID_ITASKBAR_LIST3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"
# номера методов в таблице ITaskbarList3 (после IUnknown и ITaskbarList/ITaskbarList2)
_HR_INIT, _SET_PROGRESS_VALUE, _SET_PROGRESS_STATE = 3, 9, 10


class TaskbarProgress:
    def __init__(self, hwnd: int):
        self._hwnd = hwnd
        self._object = None
        if sys.platform != "win32":
            return
        try:
            import ctypes

            self._ctypes = ctypes
            ole32 = ctypes.windll.ole32
            ole32.CoInitialize(None)  # Qt уже инициализировал COM — тогда вызов безвреден

            class GUID(ctypes.Structure):
                _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]

            clsid, iid = GUID(), GUID()
            ole32.CLSIDFromString(_CLSID_TASKBAR_LIST, ctypes.byref(clsid))
            ole32.CLSIDFromString(_IID_ITASKBAR_LIST3, ctypes.byref(iid))
            pointer = ctypes.c_void_p()
            if ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(pointer)) != 0:
                return
            self._object = pointer
            self._call(_HR_INIT, [])
        except Exception:
            self._object = None

    def _call(self, index: int, args: list[tuple]) -> None:
        ctypes = self._ctypes
        table = ctypes.cast(self._object, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *(kind for kind, _ in args))
        prototype(table[index])(self._object, *(value for _, value in args))

    def set_value(self, done: int, total: int) -> None:
        if self._object is None:
            return
        ctypes = self._ctypes
        try:
            self._call(
                _SET_PROGRESS_VALUE,
                [(ctypes.c_void_p, self._hwnd), (ctypes.c_ulonglong, done), (ctypes.c_ulonglong, total)],
            )
        except Exception:
            pass

    def set_state(self, state: int) -> None:
        if self._object is None:
            return
        ctypes = self._ctypes
        try:
            self._call(_SET_PROGRESS_STATE, [(ctypes.c_void_p, self._hwnd), (ctypes.c_int, state)])
        except Exception:
            pass
