import SwiftUI
import GusCore

struct ContentView: View {
    @Bindable var model: GusModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    HeaderView()
                    if !model.welcomed { WelcomeCard(model: model) }
                    WhatToSetCard(model: model)
                    ActionsCard(model: model)
                    OptionsCard(model: model)
                    WingCard(model: model)
                }
                .padding(20)
            }
            .frame(minWidth: 380, idealWidth: 420, maxWidth: 460)
            .background(.background)

            Divider()

            ResultsPanel(model: model)
                .frame(minWidth: 420, maxWidth: .infinity, maxHeight: .infinity)
        }
        .gusFont(13)
        .controlSize(model.textScale >= 1.25 ? .large : .regular)
        .environment(\.gusScale, model.textScale)
        .confirmationDialog(model.practice ? "Put back the gains from the last run on the pretend WING?"
                                           : "Put back the gains from the last run?",
                            isPresented: $model.confirmingUndo) {
            Button("Undo last run") { model.undo() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Any input someone changed by hand since then is left alone, and Gus will tell you which.")
        }
    }
}

// MARK: - Header and welcome

struct HeaderView: View {
    var body: some View {
        HStack(spacing: 14) {
            GusMark(size: 52)
            VStack(alignment: .leading, spacing: 2) {
                Text("Gus")
                    .gusFont(30, .bold, design: .rounded)
                Text("Your gain-staging sidekick")
                    .gusFont(14)
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(.bottom, 4)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Gus, your gain-staging sidekick")
        .accessibilityAddTraits(.isHeader)
    }
}

struct WelcomeCard: View {
    @Bindable var model: GusModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Hi, I'm Gus.")
                .gusFont(15, .semibold)
                .accessibilityAddTraits(.isHeader)
            Text("I set your WING's preamp gains by ear. Well, by meter. Pick what to set, press Listen and set gains, then play, sing or talk like it's the show. I'll say what I changed as I go.")
                .fixedSize(horizontal: false, vertical: true)
            Text("I only ever touch preamp gain, never phantom power or faders, and Undo last run puts it all back. Try Practice mode first if the console isn't here.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Button("Got it") { model.welcomed = true }
                .accessibilityLabel("Got it, hide this welcome")
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(GusColors.brandA.opacity(0.10), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(GusColors.brandA.opacity(0.35)))
    }
}

// MARK: - What to set

struct WhatToSetCard: View {
    @Bindable var model: GusModel
    var body: some View {
        Card(title: "What to set", symbol: "scope") {
            Picker("What to set", selection: $model.targetKind) {
                ForEach(TargetKind.allCases) { Text($0.title).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            switch model.targetKind {
            case .everything:
                Text("Every channel patched to a stage box or local input.")
                    .foregroundStyle(.secondary)
            case .section:
                if model.contents.sections.isEmpty {
                    Text("This show has no sections to pick from.").foregroundStyle(.secondary)
                } else {
                    LabeledRow("Section") {
                        Picker("Section", selection: $model.sectionID) {
                            ForEach(model.contents.sections) { Text($0.title).tag($0.id) }
                        }
                        .labelsHidden()
                        .accessibilityHint("A DCA from \(model.show). Gus does every channel in it.")
                    }
                }
            case .channel:
                if model.contents.channels.isEmpty {
                    Text("This show has no named channels.").foregroundStyle(.secondary)
                } else {
                    LabeledRow("Channel") {
                        Picker("Channel", selection: $model.channelID) {
                            ForEach(model.contents.channels) { Text("\($0.title)  (\($0.number))").tag($0.id) }
                        }
                        .labelsHidden()
                        .accessibilityHint("A channel from \(model.show).")
                    }
                }
            }
        }
        .disabled(model.isRunning)
    }
}

// MARK: - The big button and friends

struct ActionsCard: View {
    @Bindable var model: GusModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(spacing: 12) {
            // One button that changes between Listen and Stop, so VoiceOver focus stays on it
            // when a run starts or ends instead of falling off a button that vanished.
            Button {
                if model.isRunning { model.stop() } else { model.listenAndSet() }
            } label: {
                Label(primaryTitle, systemImage: model.isRunning ? "stop.fill" : "waveform.badge.mic")
                    .gusFont(16, .semibold)
                    .frame(maxWidth: .infinity, minHeight: 34)
            }
            .buttonStyle(.borderedProminent)
            .tint(model.isRunning ? .red : GusColors.brandA)
            .controlSize(.large)
            .disabled(!model.isRunning && !model.canRun)
            .accessibilityLabel(primaryLabel)
            .accessibilityHint(model.isRunning
                ? "Stops Gus now. Anything already changed is logged, and Undo last run puts it back. Command period."
                : runHint)
            .help(model.isRunning ? "Stop (Command-period)" : "\(runHint) (Command-Return)")

            if model.isRunning {
                HStack(spacing: 10) {
                    if reduceMotion {
                        Image(systemName: "ear").foregroundStyle(GusColors.brandA)
                    } else {
                        ProgressView().controlSize(.small)
                    }
                    Text(model.job?.busyLabel ?? "Working…")
                        .foregroundStyle(.secondary)
                    Spacer()
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(model.job?.busyLabel ?? "Working…")
                .accessibilityAddTraits(.updatesFrequently)
            }

            HStack(spacing: 10) {
                Button { model.askToUndo() } label: {
                    Label("Undo last run", systemImage: "arrow.uturn.backward")
                        .frame(maxWidth: .infinity)
                }
                .accessibilityHint("Puts back the gains from the last run. Asks first. Command Z.")
                .help("Undo last run (Command-Z)")

                Button { model.status() } label: {
                    Label("What are the gains now?", systemImage: "gauge.with.dots.needle.33percent")
                        .frame(maxWidth: .infinity)
                }
                .accessibilityHint("Reads each input's gain and phantom power, and the last run's results. Changes nothing. Command G.")
                .help("What are the gains now? (Command-G)")
            }
            .controlSize(.large)
            .disabled(model.isRunning)
        }
    }

    private var primaryTitle: String {
        if model.isRunning { return "Stop" }
        return model.dryRun ? "Listen and tell me" : "Listen and set gains"
    }

    private var primaryLabel: String {
        if model.isRunning { return "Stop" }
        return model.dryRun ? "Listen and tell me, change nothing" : "Listen and set gains"
    }

    private var runHint: String {
        let who = model.oneAtATime ? "one input at a time" : "while everyone plays"
        let what = model.dryRun ? "then says what it would change" : "then sets each preamp gain"
        return "Gus listens for \(model.listenSeconds) seconds \(who), \(what)."
    }
}

// MARK: - Options

struct OptionsCard: View {
    @Bindable var model: GusModel
    var body: some View {
        Card(title: "Options", symbol: "slider.horizontal.3") {
            if model.showNames.isEmpty {
                LabeledContent("Show") {
                    Text(model.showsLoaded ? "No shows found in Documents, WING Shows" : "Reading your shows…")
                        .foregroundStyle(.secondary)
                }
            } else {
                LabeledRow("Show") {
                    Picker("Show", selection: $model.show) {
                        ForEach(model.showNames, id: \.self) { Text($0).tag($0) }
                    }
                    .labelsHidden()
                    .accessibilityHint("Which show is loaded on the WING, so channel names match.")
                }
            }

            Divider()

            SwitchRow("Practice mode (no console needed)", isOn: $model.practice,
                      hint: "Uses a pretend WING. Nothing real changes.")
            SwitchRow("Just tell me, change nothing", isOn: $model.dryRun,
                      hint: "Measures and says what would change. Changes nothing.")
            SwitchRow("One at a time (less bleed)", isOn: $model.oneAtATime,
                      hint: "Gus names each input, then listens to it alone.")
            SwitchRow("Speak results", isOn: $model.speakResults,
                      hint: "Reads each result aloud as it arrives. Through VoiceOver when it's on.")

            Divider()

            LabeledRow("Listen for \(model.listenSeconds) seconds") {
                Stepper("Listen time", value: $model.listenSeconds, in: 2...60)
                    .labelsHidden()
                    .accessibilityValue("\(model.listenSeconds) seconds")
                    .accessibilityHint("How long Gus listens on each pass.")
            }
        }
        .toggleStyle(.switch)
        .disabled(model.isRunning)
    }
}

// MARK: - The WING's address

struct WingCard: View {
    @Bindable var model: GusModel
    var body: some View {
        Card(title: "Your WING", symbol: "network") {
            TextField("WING address", text: $model.host, prompt: Text("e.g. 192.168.68.50"))
                .textFieldStyle(.roundedBorder)
                .onSubmit { model.saveHost() }
                .accessibilityLabel("WING address")
                .accessibilityHint("The console's IP address, from Setup, Network on the WING. Return saves it.")

            HStack(spacing: 10) {
                Button { model.discover() } label: {
                    Label("Find my WING", systemImage: "magnifyingglass")
                }
                .accessibilityHint("Looks for a WING on this network and fills in its address.")
                Button { model.saveHost() } label: {
                    Label("Save", systemImage: "square.and.arrow.down")
                }
                .disabled(!model.canSaveHost)
                .accessibilityLabel("Save address")
                .accessibilityHint("Remembers this address for every run.")
                Spacer()
            }

            Text(savedText)
                .foregroundStyle(.secondary)
                .gusFont(12)
                .fixedSize(horizontal: false, vertical: true)
        }
        .disabled(model.isRunning)
    }

    private var savedText: String {
        if model.savedHost.isEmpty {
            return "No address saved yet. Practice mode doesn't need one."
        }
        return "Saved: \(model.savedHost)."
    }
}

/// A label on the left, a control on the right. The visible label is hidden from VoiceOver because
/// the control carries the same words as its own label: one stop, one complete sentence.
struct LabeledRow<Control: View>: View {
    let title: String
    @ViewBuilder var control: Control
    init(_ title: String, @ViewBuilder control: () -> Control) {
        self.title = title
        self.control = control()
    }
    var body: some View {
        HStack {
            Text(title).accessibilityHidden(true)
            Spacer(minLength: 12)
            control
        }
    }
}

/// A switch that VoiceOver reads as "Practice mode (no console needed), on, switch".
struct SwitchRow: View {
    let title: String
    @Binding var isOn: Bool
    let hint: String
    init(_ title: String, isOn: Binding<Bool>, hint: String) {
        self.title = title
        self._isOn = isOn
        self.hint = hint
    }
    var body: some View {
        Toggle(isOn: $isOn) {
            Text(title).frame(maxWidth: .infinity, alignment: .leading)
        }
        .toggleStyle(.switch)
        .accessibilityHint(hint)
    }
}
