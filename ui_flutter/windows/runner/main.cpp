#include <flutter/dart_project.h>
#include <flutter/flutter_view_controller.h>
#include <windows.h>

#include <cstdlib>
#include <cstring>

#include "flutter_window.h"
#include "utils.h"

int APIENTRY wWinMain(_In_ HINSTANCE instance, _In_opt_ HINSTANCE prev,
                      _In_ wchar_t *command_line, _In_ int show_command) {
  // Attach to console when present (e.g., 'flutter run') or create a
  // new console when running with a debugger.
  if (!::AttachConsole(ATTACH_PARENT_PROCESS) && ::IsDebuggerPresent()) {
    CreateAndAttachConsole();
  }

  // Initialize COM, so that it is available for use in the library and/or
  // plugins.
  ::CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

  flutter::DartProject project(L"data");

  std::vector<std::string> command_line_arguments =
      GetCommandLineArguments();

  project.set_dart_entrypoint_arguments(std::move(command_line_arguments));

  // Политика рендера из окружения стенда: default / on / off. Обёртка
  // даёт DefaultImpeller, если не выставить - сравнение на аргументах
  // командной строки ничего не доказывало.
  char impeller_buf[16];
  if (::GetEnvironmentVariableA("OMNISTASH_IMPELLER", impeller_buf,
                                sizeof(impeller_buf)) > 0) {
    if (::strcmp(impeller_buf, "on") == 0) {
      project.set_impeller_switch(flutter::ImpellerSwitch::Enabled);
    } else if (::strcmp(impeller_buf, "off") == 0) {
      project.set_impeller_switch(flutter::ImpellerSwitch::Disabled);
    } else {
      project.set_impeller_switch(flutter::ImpellerSwitch::Default);
    }
  }

  FlutterWindow window(project);
  Win32Window::Point origin(10, 10);
  // Размер окна берётся из окружения стенда: 1280x720 - базовый,
  // 1600x900 - эквивалент 125% по числу пикселей (1.5625x), 1920x1080 -
  // эквивалент 150% (2.25x). Системный DPI не меняется: это измерение
  // без правки настроек человека. Win32 API вместо getenv - MSVC считает
  // getenv deprecated и ломает сборку warning-as-error.
  int width = 1280;
  int height = 720;
  char buf[16];
  if (::GetEnvironmentVariableA("OMNISTASH_SPIKE_W", buf, sizeof(buf)) > 0) {
    width = ::atoi(buf);
  }
  if (::GetEnvironmentVariableA("OMNISTASH_SPIKE_H", buf, sizeof(buf)) > 0) {
    height = ::atoi(buf);
  }
  Win32Window::Size size(width, height);
  if (!window.Create(L"omnistash_ui", origin, size)) {
    return EXIT_FAILURE;
  }
  window.SetQuitOnClose(true);

  ::MSG msg;
  while (::GetMessage(&msg, nullptr, 0, 0)) {
    ::TranslateMessage(&msg);
    ::DispatchMessage(&msg);
  }

  ::CoUninitialize();
  return EXIT_SUCCESS;
}
