import 'package:flutter/material.dart';

import 'core/theme.dart';

/// Nightshift Security mobile app — PHASE 0 shell.
///
/// Screens are built phase by phase (spec §41, §52); the app is deliberately not
/// started before the device pipeline works end to end.
void main() {
  runApp(const NightshiftApp());
}

class NightshiftApp extends StatelessWidget {
  const NightshiftApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Nightshift Security',
      debugShowCheckedModeBanner: false,
      theme: NightshiftTheme.dark(),
      home: const PhaseStatusScreen(),
    );
  }
}

/// Placeholder home screen: states plainly what is and is not implemented.
class PhaseStatusScreen extends StatelessWidget {
  const PhaseStatusScreen({super.key});

  static const _phases = <(String, String, bool)>[
    ('PHASE 0', 'Monorepo, servisler, tema iskeleti', true),
    ('PHASE 1', 'Dahua donanım probe (edge-agent)', true),
    ('PHASE 2', 'Edge kayıt + heartbeat', false),
    ('PHASE 4', 'Olay akışı ve snapshot', false),
    ('PHASE 7', 'Push, alarm, ACK', false),
    ('PHASE 9', 'Canlı izleme', false),
  ];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Nightshift Security')),
      body: ListView(
        padding: const EdgeInsets.symmetric(vertical: 12),
        children: [
          const Padding(
            padding: EdgeInsets.fromLTRB(16, 8, 16, 16),
            child: Text(
              'Uygulama kabuğu hazır. Ekranlar, cihaz hattı doğrulandıkça eklenir.',
              style: TextStyle(color: NightshiftTheme.textMuted),
            ),
          ),
          for (final (phase, title, done) in _phases)
            Card(
              child: ListTile(
                leading: Icon(
                  done ? Icons.check_circle : Icons.radio_button_unchecked,
                  color: done
                      ? NightshiftTheme.severityLow
                      : NightshiftTheme.textMuted,
                ),
                title: Text(title),
                subtitle: Text(
                  phase,
                  style: const TextStyle(color: NightshiftTheme.textMuted),
                ),
              ),
            ),
        ],
      ),
    );
  }
}
