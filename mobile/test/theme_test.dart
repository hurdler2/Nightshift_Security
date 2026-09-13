import 'package:flutter_test/flutter_test.dart';
import 'package:nightshift/core/theme.dart';

void main() {
  group('severity mapping (spec 18)', () {
    test('risk score bands', () {
      expect(NightshiftTheme.severityForRisk(0), 'INFO');
      expect(NightshiftTheme.severityForRisk(29), 'INFO');
      expect(NightshiftTheme.severityForRisk(30), 'LOW');
      expect(NightshiftTheme.severityForRisk(49), 'LOW');
      expect(NightshiftTheme.severityForRisk(50), 'MEDIUM');
      expect(NightshiftTheme.severityForRisk(69), 'MEDIUM');
      expect(NightshiftTheme.severityForRisk(70), 'HIGH');
      expect(NightshiftTheme.severityForRisk(84), 'HIGH');
      expect(NightshiftTheme.severityForRisk(85), 'CRITICAL');
      expect(NightshiftTheme.severityForRisk(100), 'CRITICAL');
    });

    test('critical is visually distinct from info', () {
      expect(
        NightshiftTheme.forSeverity('CRITICAL'),
        isNot(NightshiftTheme.forSeverity('INFO')),
      );
    });
  });
}
