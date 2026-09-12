// Smoke test for the CloudLens app shell.
//
// MyApp resolves the current Amplify session before deciding whether to show
// LoginPage or MainPage. Amplify is not configured inside a widget test, so
// this test asserts on the first frame, which is rendered while that session
// lookup is still pending.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:cloud_lens/main.dart';

void main() {
  testWidgets('shows a loading indicator while the session is resolved', (
    WidgetTester tester,
  ) async {
    await tester.pumpWidget(const MyApp());

    expect(find.byType(CircularProgressIndicator), findsOneWidget);
  });

  testWidgets('builds a MaterialApp titled Cloud Lens', (
    WidgetTester tester,
  ) async {
    await tester.pumpWidget(const MyApp());

    final MaterialApp app = tester.widget(find.byType(MaterialApp));
    expect(app.title, 'Cloud Lens');
  });
}
