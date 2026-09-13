import 'package:flutter/material.dart';

/// Dark "security console" theme (spec §42).
///
/// Severity colours are the load-bearing part of this file: a CRITICAL alarm has to
/// be unmistakable on a phone held at arm's length, at night, on a construction site.
class NightshiftTheme {
  static const Color background = Color(0xFF0B0F14);
  static const Color surface = Color(0xFF141A22);
  static const Color surfaceAlt = Color(0xFF1C242F);
  static const Color accent = Color(0xFF2F9BFF);
  static const Color textPrimary = Color(0xFFE8EDF2);
  static const Color textMuted = Color(0xFF8C9AAB);

  // Severity palette (spec §18).
  static const Color severityInfo = Color(0xFF5B7085);
  static const Color severityLow = Color(0xFF3EA76A);
  static const Color severityMedium = Color(0xFFE0A32E);
  static const Color severityHigh = Color(0xFFF06A2A);
  static const Color severityCritical = Color(0xFFE4342F);

  static Color forSeverity(String severity) => switch (severity.toUpperCase()) {
        'CRITICAL' => severityCritical,
        'HIGH' => severityHigh,
        'MEDIUM' => severityMedium,
        'LOW' => severityLow,
        _ => severityInfo,
      };

  /// Severity band for a 0..100 risk score (spec §18).
  static String severityForRisk(int riskScore) {
    if (riskScore >= 85) return 'CRITICAL';
    if (riskScore >= 70) return 'HIGH';
    if (riskScore >= 50) return 'MEDIUM';
    if (riskScore >= 30) return 'LOW';
    return 'INFO';
  }

  static ThemeData dark() {
    final scheme = ColorScheme.fromSeed(
      seedColor: accent,
      brightness: Brightness.dark,
    ).copyWith(
      surface: surface,
      error: severityCritical,
    );

    return ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      colorScheme: scheme,
      scaffoldBackgroundColor: background,
      appBarTheme: const AppBarTheme(
        backgroundColor: background,
        foregroundColor: textPrimary,
        elevation: 0,
        centerTitle: false,
      ),
      cardTheme: CardTheme(
        color: surface,
        elevation: 0,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
        margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      ),
      listTileTheme: const ListTileThemeData(textColor: textPrimary),
      dividerColor: surfaceAlt,
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          minimumSize: const Size.fromHeight(52), // one-tap ACK, glove friendly
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        ),
      ),
    );
  }
}
